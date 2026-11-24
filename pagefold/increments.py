import json

from .catalog import describe
from .codec import fingerprint
from .constraints import check_database
from .errors import Problem
from .releases import get_release
from .replay import apply_rows
from .rows import read_image
from .sqlite_image import mounted, validate_header

FORMAT = "pagefold-increment-v1"


def logical_digest(rows):
    return fingerprint(rows)


def diff_tables(before, after):
    tables = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, {}), after.get(name, {})
        changed = [key for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]
        if changed:
            tables.append({"table": name,
                           "delete": [json.loads(key) for key in changed if key in old],
                           "insert": [new[key] for key in changed if key in new]})
    return tables


def release_snapshot(store, plan_id):
    with store.connect() as db:
        release = get_release(db, plan_id, body=True)
    certificate = json.loads(release["certificate"])
    with mounted(release["candidate"]) as image:
        catalog = describe(image)
        if catalog["signature"] != certificate["schema"]:
            raise Problem("certificate_schema", "封存凭证的模式摘要与候选不一致。")
        rows = read_image(image, catalog)
    return certificate, rows, release["candidate"]


def endpoint(plan_id, certificate, rows):
    return {"plan": plan_id, "revision": certificate["revision"],
            "candidate_digest": certificate["candidate_digest"],
            "logical": logical_digest(rows), "rows": sum(map(len, rows.values()))}


def build_increment(store, from_plan, to_plan):
    old_cert, old_rows, old_candidate = release_snapshot(store, from_plan)
    new_cert, new_rows, _ = release_snapshot(store, to_plan)
    if old_cert["schema"] != new_cert["schema"]:
        raise Problem("schema_drift", "两版封存成果的模式不同，前进增量只承载数据变化。", 409)
    increment = {"format": FORMAT, "schema": old_cert["schema"],
                 "from": endpoint(from_plan, old_cert, old_rows),
                 "to": endpoint(to_plan, new_cert, new_rows),
                 "tables": diff_tables(old_rows, new_rows)}
    apply_increment(old_candidate, increment)
    return increment


def expect_shape(increment):
    if not isinstance(increment, dict) or increment.get("format") != FORMAT:
        raise Problem("increment_format", "增量文档格式无法识别。")
    ends = [increment.get(side) for side in ("from", "to")]
    if not isinstance(increment.get("schema"), str) or not isinstance(increment.get("tables"), list) \
            or any(not isinstance(end, dict) or not isinstance(end.get("logical"), str) for end in ends):
        raise Problem("increment_format", "增量文档缺少必要的摘要字段。")
    for entry in increment["tables"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("table"), str) \
                or not isinstance(entry.get("delete"), list) or not isinstance(entry.get("insert"), list):
            raise Problem("increment_format", "增量文档的行操作无法识别。")


def operations(catalog, increment):
    tables = {table["name"]: table for table in catalog["tables"]}
    result = {}
    for entry in increment["tables"]:
        table = tables.get(entry["table"])
        if table is None:
            raise Problem("increment_table", f"增量引用了目录中不存在的表 {entry['table']}。")
        positions = [table["columns"].index(key) for key in table["key"]]
        changes = [({"key": key, "base": True}, None) for key in entry["delete"]]
        changes += [({"key": [row[pos] for pos in positions], "base": None}, row)
                    for row in entry["insert"]]
        result[table["name"]] = changes
    return result


def apply_increment(data, increment):
    expect_shape(increment)
    validate_header(data)
    with mounted(data, writable=True) as db:
        catalog = describe(db)
        if catalog["signature"] != increment["schema"]:
            raise Problem("schema_mismatch", "本地镜像的模式与增量记录不符。", 409)
        if logical_digest(read_image(db, catalog)) != increment["from"]["logical"]:
            raise Problem("base_mismatch", "本地数据与增量起点不一致，未做任何更改。", 409)
        changes = operations(catalog, increment)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for table in catalog["tables"]:
            if table["name"] in changes:
                apply_rows(db, table, changes[table["name"]])
        check_database(db)
        if logical_digest(read_image(db, catalog)) != increment["to"]["logical"]:
            raise Problem("target_mismatch", "应用结果与增量目标不一致。")
        db.commit()
        result = db.serialize()
    validate_header(result)
    return result
