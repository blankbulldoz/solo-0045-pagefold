from .codec import fingerprint
from .merge import ROLES, selected

IMPACT_FORMAT = "pagefold-impact-v1"
LEGACY_IMPACT = {"format": "pagefold-impact-unknown", "scope": "unknown",
                 "detail": "旧版凭证未记录逐表影响事实，可校验范围仅限候选字节摘要。"}
WRITE_KINDS = ("insert", "update", "delete", "none")
BRANCH_ROLES = ("left", "right")


def write_kind(base_row, selected_row):
    """候选库相对祖本的最终变动：插入、修改、删除或无写入。"""
    if selected_row == base_row:
        return "none"
    if base_row is None:
        return "insert"
    if selected_row is None:
        return "delete"
    return "update"


def classify(item, decisions):
    role, row = selected(item, decisions)
    if role is None:
        raise ValueError("impact report requires a fully resolved plan")
    return {
        "table": item["table"],
        "key": item["key"],
        "origin": "automatic" if item["automatic"] is not None else "manual",
        "proposals": [name for name in BRANCH_ROLES if item[name] != item["base"]],
        "choice": role,
        "write": write_kind(item["base"], row),
    }


def blank_table():
    return {"rows": 0, "rows_digest": fingerprint([]),
            "writes": {kind: 0 for kind in WRITE_KINDS},
            "items": 0, "origins": {"automatic": 0, "manual": 0},
            "proposals": {"left": 0, "right": 0, "both": 0},
            "choices": {role: 0 for role in ROLES}}


def impact_report(items, decisions, catalog, final_snapshot):
    """依据计划、裁决与解包后的候选数据库构造可校验影响事实。"""
    tables = {table["name"]: blank_table() for table in catalog["tables"]}
    totals = {"items": 0, "written_tables": 0,
              "writes": {kind: 0 for kind in WRITE_KINDS},
              "origins": {"automatic": 0, "manual": 0},
              "proposals": {"left": 0, "right": 0, "both": 0},
              "choices": {role: 0 for role in ROLES}}
    facts = []
    for item in items:
        fact = classify(item, decisions)
        facts.append(fact)
        stats = tables[fact["table"]]
        stats["items"] += 1
        stats["origins"][fact["origin"]] += 1
        stats["choices"][fact["choice"]] += 1
        stats["writes"][fact["write"]] += 1
        proposal_key = "both" if len(fact["proposals"]) == 2 else fact["proposals"][0]
        stats["proposals"][proposal_key] += 1
        totals["items"] += 1
        totals["origins"][fact["origin"]] += 1
        totals["choices"][fact["choice"]] += 1
        totals["writes"][fact["write"]] += 1
        totals["proposals"][proposal_key] += 1
    for name, stats in tables.items():
        rows = final_snapshot.get(name, {})
        stats["rows"] = len(rows)
        stats["rows_digest"] = fingerprint(sorted(rows.items()))
    written = sorted(name for name, stats in tables.items()
                     if any(stats["writes"][kind] for kind in ("insert", "update", "delete")))
    totals["written_tables"] = len(written)
    return {"format": IMPACT_FORMAT,
            "rows_digest": fingerprint({name: stats["rows_digest"] for name, stats in tables.items()}),
            "written_tables": written, "totals": totals,
            "tables": tables, "items": facts}


def impact_from_candidate(candidate, catalog, items, decisions):
    """直接解包候选字节重读全部行，供封存时独立计算影响事实。"""
    from .rows import read_image
    from .sqlite_image import mounted

    with mounted(candidate) as db:
        snapshot = read_image(db, catalog)
    return impact_report(items, decisions, catalog, snapshot)
