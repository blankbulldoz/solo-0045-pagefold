import json
import uuid

from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES, calculate, selected
from .rows import read_image
from .scope import analyze
from .sqlite_image import mounted


def normalize_scope(scope, catalog):
    if scope is None:
        return None
    if not isinstance(scope, list) or not scope or not all(isinstance(name, str) for name in scope):
        raise Problem("scope", "范围须是非空的表名数组，或省略以纳入全部表。", 400)
    names = [table["name"] for table in catalog["tables"]]
    if len(set(scope)) != len(scope) or any(name not in names for name in scope):
        raise Problem("scope", "范围含有重复或不存在的表名。", 400)
    return list(scope)


def scope_report(row, base_image=None):
    items, decisions = plan_content(row)
    catalog = json.loads(base_image["catalog"])
    scope = names_of(catalog) if row["scope"] is None else json.loads(row["scope"])
    with mounted(base_image["body"]) as snapshot:
        base_snapshot = read_image(snapshot, catalog)
    report = analyze(catalog, base_snapshot, items, decisions, scope,
                     json.loads(row["amendments"]))
    report["full_schema"] = row["scope"] is None
    return report


def names_of(catalog):
    return [table["name"] for table in catalog["tables"]]


def create_plan(store, sources, scope=None):
    with store.connect() as db:
        images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
    scope = normalize_scope(scope, catalogs[0])
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    items = calculate(*snapshots)
    plan_id = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO plans(id,base,left_id,right_id,items,scope,amendments) "
            "VALUES(?,?,?,?,?,?,?)",
            (plan_id, *[sources[role] for role in ROLES], canonical(items),
             canonical(scope) if scope is not None else None, "[]"))
        return read_plan(db, plan_id)


def get_plan(db, plan_id):
    row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unknown_plan", "找不到指定的合流计划。", 404)
    return row


def plan_content(row):
    return json.loads(row["items"]), json.loads(row["decisions"])


def read_plan(db, plan_id):
    row = get_plan(db, plan_id)
    items, decisions = plan_content(row)
    sources = {"base": row["base"], "left": row["left_id"], "right": row["right_id"]}
    base = get_image(db, row["base"], body=True)
    report = scope_report(row, base)
    return {"id": plan_id, "sources": sources, "revision": row["revision"], "state": row["state"],
            "plan_digest": fingerprint(items),
            "unresolved": sum(1 for entry in report["active"] if entry["choice"] is None),
            "scope_blocked": len(report["blocked"]),
            "scope_report": report,
            "items": [dict(i, choice=selected(i, decisions)[0]) for i in items]}


def list_plans(db):
    return [dict(row) for row in db.execute("""
        SELECT p.id,p.revision,p.state,b.label AS base_label,l.label AS left_label,r.label AS right_label
        FROM plans p JOIN images b ON b.id=p.base JOIN images l ON l.id=p.left_id
        JOIN images r ON r.id=p.right_id ORDER BY p.rowid DESC
    """)]


def expect_revision(plan, revision):
    if plan["revision"] != revision:
        raise Problem("stale_revision", "计划版本已改变，请刷新后再操作。", 409)


def expect_open(plan):
    if plan["state"] != "open":
        raise Problem("closed_plan", "封存或归档后的计划不可继续编辑或重演。", 409)
