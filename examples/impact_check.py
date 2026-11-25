"""独立于封存逻辑的影响事实核验工具。

只依赖公开的编码与行扫描规则，解包候选 SQLite 后逐表重读，
重新计算行摘要，与凭证中的影响事实逐项对账。
"""
from pagefold.codec import canonical, fingerprint
from pagefold.rows import read_image
from pagefold.sqlite_image import mounted


def recompute(candidate_bytes, catalog):
    with mounted(candidate_bytes) as db:
        snapshot = read_image(db, catalog)
    tables = {}
    for table in catalog["tables"]:
        rows = snapshot.get(table["name"], {})
        tables[table["name"]] = {"rows": len(rows), "rows_digest": fingerprint(sorted(rows.items()))}
    rows_digest = fingerprint({name: stats["rows_digest"] for name, stats in tables.items()})
    return snapshot, tables, rows_digest


def verify(certificate, candidate_bytes, catalog, plan_items):
    """返回 (错误列表, 核验明细)。"""
    errors = []
    impact = certificate["impact"]
    snapshot, tables, rows_digest = recompute(candidate_bytes, catalog)
    if impact["format"] != "pagefold-impact-v1":
        errors.append(f"影响事实格式未知：{impact.get('format')}")
        return errors, {"rows_digest": rows_digest}
    if rows_digest != impact["rows_digest"]:
        errors.append("整库行摘要不一致")
    for name, stats in impact["tables"].items():
        actual = tables.get(name)
        if actual is None:
            errors.append(f"凭证包含目录外的表 {name}")
            continue
        if stats["rows_digest"] != actual["rows_digest"]:
            errors.append(f"表 {name} 行摘要不一致")
        if stats["rows"] != actual["rows"]:
            errors.append(f"表 {name} 行数不一致：{stats['rows']} != {actual['rows']}")
    for name in tables:
        if name not in impact["tables"]:
            errors.append(f"凭证缺少表 {name} 的影响事实")
    # written_tables 必须与逐表写入计数一致
    counted = sorted(name for name, stats in impact["tables"].items()
                     if any(stats["writes"][kind] for kind in ("insert", "update", "delete")))
    if counted != impact["written_tables"]:
        errors.append(f"写过表集合与计数不一致：{counted} != {impact['written_tables']}")
    # 凭证事实中的每个键必须能在计划项中找到且账实相符
    items = {(item["table"], canonical(item["key"])): item for item in plan_items}
    for fact in impact["items"]:
        marker = (fact["table"], canonical(fact["key"]))
        if marker not in items:
            errors.append(f"影响事实包含计划外的键 {marker}")
            continue
        item = items[marker]
        present = canonical(fact["key"]) in snapshot.get(fact["table"], {})
        if fact["write"] == "delete" and present:
            errors.append(f"{marker} 标注删除但候选中仍存在")
        if fact["write"] in {"insert", "update"} and not present:
            errors.append(f"{marker} 标注 {fact['write']} 但候选中缺席")
        if fact["write"] == "none" and item["base"] is not None and not present:
            errors.append(f"{marker} 标注无写入但候选中缺席")
    if len(impact["items"]) != impact["totals"]["items"]:
        errors.append("影响事实条目数与总计不符")
    return errors, {"rows_digest": rows_digest, "tables": tables,
                    "snapshot": snapshot, "counted_written": counted}
