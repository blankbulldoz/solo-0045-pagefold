"""两种镜像身份与类型化对照。

- 文件身份：``images.id``，精确字节的 SHA-256，VACUUM 后必然可能改变。
- 内容身份：``content_digest``，绑定模式签名和带存储类型标记的全部行，
  碎片整理（VACUUM）不改变它，而整数/文本、NULL/空 BLOB 的差异一定会改变它。

本模块只做派生计算，不改变登记主键、计划来源和封存凭证中绑定的精确文件摘要。
"""

import json
import sqlite3

from .codec import fingerprint
from .errors import Problem
from .merge import ROLES
from .rows import read_image
from .sqlite_image import mounted

KIND_LABELS = {"n": "NULL", "i": "整数", "r": "实数", "t": "文本", "b": "BLOB"}

ROLE_COLUMNS = {"base": "base", "left": "left_id", "right": "right_id"}

VERDICT_LABELS = {
    "same_file": "同一文件",
    "defragmented_equivalent": "碎片整理后等价",
    "type_shifted_lookalike": "行位置相同但类型不同（存储类型不一致）",
    "different": "内容不同",
}


def kind_label(atom):
    if atom is None:
        return "缺席"
    if atom[0] == "b" and not atom[1]:
        return "空 BLOB"
    return KIND_LABELS[atom[0]]


def content_identity(signature, rows):
    """模式签名 + 全部类型化行的稳定摘要。

    行值已经过 ``codec.atom`` 保真编码：``["i","1"]`` 与 ``["t","1"]``、
    ``["n"]`` 与 ``["b",""]`` 都不相等，因此不会被行数或显示文本混淆。
    """
    return fingerprint({"v": 1, "schema": signature, "tables": rows})


def pack_body(body, catalog):
    with mounted(body) as db:
        rows = read_image(db, catalog)
    return {"catalog": catalog, "rows": rows, "content_digest": content_identity(catalog["signature"], rows)}


def pack_row(row):
    catalog = json.loads(row["catalog"])
    packed = pack_body(row["body"], catalog)
    packed.update(id=row["id"], label=row["label"])
    if row["content_digest"]:
        packed["content_digest"] = row["content_digest"]
    return packed


def brief(row):
    return {"id": row["id"], "label": row["label"], "size": row["size"],
            "content_digest": row["content_digest"]}


def compare_packs(left, right):
    """按 ordinal 行位置逐列比较两份类型化镜像。

    ``positions_match`` 表示表集合一致且每表行数相同，可按行位置配对；
    ``key_positions_match`` 进一步表示有序业务键编码（含类型）完全一致。
    每个单元格差异都带双方存储类型，以区分“值不同”和“类型不同”。
    """
    left_tables = {table["name"]: table for table in left["catalog"]["tables"]}
    right_tables = {table["name"]: table for table in right["catalog"]["tables"]}
    names = sorted(set(left_tables) | set(right_tables))
    same_schema = left["catalog"]["signature"] == right["catalog"]["signature"]
    same_table_set = set(left_tables) == set(right_tables)
    tables = []
    type_changes = []
    type_change_count = 0
    value_change_count = 0
    ordinal_positions_match = True
    key_positions_overall = True
    for name in names:
        ltable, rtable = left_tables.get(name), right_tables.get(name)
        lordered = list(left["rows"].get(name, {}).items())
        rordered = list(right["rows"].get(name, {}).items())
        lkeys = [key for key, _ in lordered]
        rkeys = [key for key, _ in rordered]
        key_positions_match = lkeys == rkeys
        equal_row_count = len(lordered) == len(rordered)
        # “行位置相同”按 ordinal 对齐：表集合一致且每表行数一致即可逐行配对；
        # key_positions_match 额外表示业务键编码（含类型）也完全一致。
        table_positions_match = equal_row_count and ltable is not None and rtable is not None
        if not table_positions_match:
            ordinal_positions_match = False
        if not key_positions_match:
            key_positions_overall = False
        columns = (ltable or rtable)["columns"]
        left_width = len(ltable["columns"]) if ltable else 0
        right_width = len(rtable["columns"]) if rtable else 0
        shared_columns = min(len(columns), left_width, right_width) or len(columns)
        differences = []
        for position in range(max(len(lordered), len(rordered))):
            if position >= len(lordered) or position >= len(rordered):
                differences.append({"position": position, "column": None, "type_change": False,
                                    "presence": "right_only" if position >= len(lordered) else "left_only"})
                continue
            lkey_token, lrow = lordered[position]
            rkey_token, rrow = rordered[position]
            key_changed = lkey_token != rkey_token
            key = json.loads(lkey_token)
            if key_changed:
                key = {"left": json.loads(lkey_token), "right": json.loads(rkey_token)}
            for index in range(min(shared_columns, len(lrow), len(rrow))):
                lvalue, rvalue = lrow[index], rrow[index]
                if lvalue == rvalue:
                    continue
                is_type_change = lvalue[0] != rvalue[0]
                change = {"position": position, "key": key, "key_changed": key_changed,
                          "column": columns[index] if index < len(columns) else f"#{index + 1}",
                          "a": lvalue, "b": rvalue,
                          "kind_a": kind_label(lvalue), "kind_b": kind_label(rvalue),
                          "type_change": is_type_change}
                differences.append(change)
                if is_type_change:
                    type_change_count += 1
                    if len(type_changes) < 24:
                        type_changes.append({"table": name, **change})
                else:
                    value_change_count += 1
        tables.append({"table": name, "rows_a": len(lordered), "rows_b": len(rordered),
                       "positions_match": table_positions_match,
                       "key_positions_match": key_positions_match,
                       "difference_count": len(differences),
                       "type_change_count": sum(1 for d in differences if d["type_change"]),
                       "differences": differences[:100]})
    return {"same_schema": same_schema, "same_table_set": same_table_set,
            "positions_match": ordinal_positions_match,
            "ordinal_positions_match": ordinal_positions_match,
            "key_positions_match": key_positions_overall,
            "type_change_count": type_change_count, "value_change_count": value_change_count,
            "type_changes": type_changes, "tables": tables}


def verdict_for(ida, idb, digest_a, digest_b, comparison):
    if ida == idb:
        return "same_file"
    if digest_a == digest_b:
        return "defragmented_equivalent"
    if comparison["type_change_count"] and comparison["positions_match"]:
        return "type_shifted_lookalike"
    return "different"


def compare_dossier(db, left_id, right_id):
    from .images import get_image

    left_row = get_image(db, left_id, body=True)
    right_row = get_image(db, right_id, body=True)
    left, right = pack_row(left_row), pack_row(right_row)
    comparison = compare_packs(left, right)
    digest_a = left_row["content_digest"] or left["content_digest"]
    digest_b = right_row["content_digest"] or right["content_digest"]
    comparison.update({
        "a": brief(left_row), "b": brief(right_row),
        "byte_identical": left_id == right_id,
        "content_equivalent": bool(digest_a and digest_a == digest_b),
        "verdict": verdict_for(left_id, right_id, digest_a, digest_b, comparison),
        "verdict_label": VERDICT_LABELS[verdict_for(left_id, right_id, digest_a, digest_b, comparison)],
    })
    return comparison


def equivalent_images(db, image_id, content_digest):
    if not content_digest:
        return []
    return [dict(row) for row in db.execute(
        "SELECT id,label,content_digest,length(body) AS size FROM images "
        "WHERE content_digest=? AND id<>? ORDER BY rowid DESC", (content_digest, image_id))]


def type_lookalikes(db, image_id, record, own_catalog, own_rows, limit=12):
    """同模式、同行数且行位置对齐，但单元格存储类型不同的已登记镜像。"""
    signature = own_catalog["signature"]
    own_counts = {table["name"]: table.get("rows", len(own_rows.get(table["name"], {})))
                  for table in own_catalog["tables"]}
    candidates = db.execute(
        "SELECT id,label,content_digest,catalog FROM images WHERE id<>? ORDER BY rowid DESC",
        (image_id,)).fetchall()
    found = []
    own_pack = {"catalog": own_catalog, "rows": own_rows}
    for candidate in candidates:
        try:
            catalog = json.loads(candidate["catalog"])
        except (ValueError, TypeError):
            continue
        if catalog.get("signature") != signature:
            continue
        counts = {table["name"]: table.get("rows") for table in catalog["tables"]}
        if counts != own_counts or candidate["content_digest"] == record["content_digest"]:
            continue
        body_row = db.execute("SELECT body FROM images WHERE id=?", (candidate["id"],)).fetchone()
        try:
            other = pack_body(body_row["body"], catalog)
        except (sqlite3.Error, Problem, ValueError, TypeError):
            continue
        comparison = compare_packs(own_pack, other)
        if comparison["type_change_count"] and comparison["positions_match"]:
            found.append({"id": candidate["id"], "label": candidate["label"],
                          "content_digest": candidate["content_digest"],
                          "type_change_count": comparison["type_change_count"],
                          "value_change_count": comparison["value_change_count"],
                          "type_changes": [
                              {"table": change["table"], "column": change["column"],
                               "position": change["position"], "kind_a": change["kind_a"],
                               "kind_b": change["kind_b"]}
                              for change in comparison["type_changes"][:8]],
                          "compare": f"/images/compare?a={image_id}&b={candidate['id']}"})
        if len(found) >= limit:
            break
    return found


def image_usage(db, image_id):
    plans = db.execute(
        "SELECT id,state,revision,base,left_id,right_id FROM plans "
        "WHERE ? IN (base,left_id,right_id) ORDER BY rowid DESC", (image_id,)).fetchall()
    used = []
    for plan in plans:
        roles = [role for role in ROLES if plan[ROLE_COLUMNS[role]] == image_id]
        certificate = db.execute("SELECT certificate FROM releases WHERE plan_id=?",
                                (plan["id"],)).fetchone()
        cert_info = None
        if certificate is not None:
            document = json.loads(certificate["certificate"])
            cert_info = {"revision": document.get("revision"),
                         "candidate_digest": document.get("candidate_digest"),
                         "url": f"/api/plans/{plan['id']}/certificate"}
        used.append({"id": plan["id"], "state": plan["state"], "revision": plan["revision"],
                     "roles": roles, "certificate": cert_info,
                     "url": f"/plans/{plan['id']}"})
    return used


def equivalent_usage(db, equivalents):
    """内容等价的其他文件身份上已发生的计划与凭证（精确来源仍归属各自文件摘要）。"""
    history = []
    for equivalent in equivalents:
        for used in image_usage(db, equivalent["id"]):
            history.append({**used, "image_id": equivalent["id"], "image_label": equivalent["label"]})
    return history


def backfill_content_identity(store):
    """为旧状态库中没有内容摘要列值的登记件补算身份。"""
    from .images import get_image

    with store.connect(write=True) as db:
        pending = [row["id"] for row in db.execute(
            "SELECT id FROM images WHERE content_digest IS NULL").fetchall()]
        for image_id in pending:
            row = get_image(db, image_id, body=True)
            catalog = json.loads(row["catalog"])
            packed = pack_body(row["body"], catalog)
            db.execute("UPDATE images SET content_digest=? WHERE id=?",
                       (packed["content_digest"], image_id))
    return len(pending)
