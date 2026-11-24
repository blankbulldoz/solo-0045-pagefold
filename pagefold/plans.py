import json
import uuid

from .bridge import compute, migrate_ancestor, translate
from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image, register
from .merge import ROLES, calculate, selected
from .rows import read_image
from .sqlite_image import mounted


def _snapshots(images, catalogs):
    result = []
    for role in ROLES:
        with mounted(images[role]["body"]) as snapshot:
            result.append(read_image(snapshot, catalogs[role]))
    return result


def _store_plan(store, sources, items, bridge=None):
    plan_id = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute("INSERT INTO plans(id,base,left_id,right_id,items,bridge) VALUES(?,?,?,?,?,?)",
                   (plan_id, *[sources[role] for role in ROLES], canonical(items),
                    canonical(bridge) if bridge else None))
        return read_plan(db, plan_id)


def create_plan(store, sources, declaration=None, trial=False):
    with store.connect() as db:
        images = {role: get_image(db, sources[role], body=True) for role in ROLES}
    catalogs = {role: json.loads(images[role]["catalog"]) for role in ROLES}
    if declaration is None:
        if len({catalog["signature"] for catalog in catalogs.values()}) != 1:
            raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
        return _store_plan(store, sources, calculate(*_snapshots(images, catalogs)))
    result = compute(catalogs, declaration)
    snapshots = {}
    for role in ROLES:
        with mounted(images[role]["body"]) as snapshot:
            rows = read_image(snapshot, catalogs[role])
        snapshots[role] = translate(result, role, rows)
    report = result["report"]
    unreachable = report["unreachable"]
    if unreachable:
        if trial:
            return {"trial": True, "reachable": False, "bridge": report}
        reasons = "；".join(f"{item['table']} 的 {item['column'] or '表'}（{item['role']}）：{item['reason']}"
                           for item in unreachable[:3])
        raise Problem("bridge_unreachable",
                      f"桥接存在无法合流的结构：{reasons}。共 {len(unreachable)} 处，详见试算结果。", 409)
    items = calculate(snapshots["base"], snapshots["left"], snapshots["right"])
    migrated, alterations = migrate_ancestor(images["base"]["body"], result)
    with mounted(migrated) as check:
        preserved = read_image(check, result["catalog"])
    report["alterations"] = alterations
    report["identity"]["ancestor_rows_preserved"] = preserved == snapshots["base"]
    if not report["identity"]["ancestor_rows_preserved"]:
        raise Problem("bridge_identity", "迁移后的祖本行与桥接结果不一致。", 500)
    if trial:
        return {"trial": True, "reachable": True, "bridge": report, "items": items,
                "unresolved": sum(item["automatic"] is None for item in items)}
    ancestor = register(store, migrated, f"桥接祖本（{images['base']['label']}）")
    bridge = {"target": result["target"], "declaration": declaration, "catalog": result["catalog"],
              "ancestor": ancestor["id"], "schema": report["schema"], "report": report,
              "digest": fingerprint(declaration)}
    return _store_plan(store, sources, items, bridge)


def get_plan(db, plan_id):
    row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unknown_plan", "找不到指定的合流计划。", 404)
    return row


def plan_content(row):
    return json.loads(row["items"]), json.loads(row["decisions"])


def plan_bridge(row):
    return json.loads(row["bridge"]) if row["bridge"] else None


def read_plan(db, plan_id):
    row = get_plan(db, plan_id)
    items, decisions = plan_content(row)
    sources = {"base": row["base"], "left": row["left_id"], "right": row["right_id"]}
    bridge = plan_bridge(row)
    summary = None
    if bridge:
        summary = {key: bridge[key] for key in ("target", "declaration", "ancestor", "schema",
                                                "digest", "report")}
    return {"id": plan_id, "sources": sources, "revision": row["revision"], "state": row["state"],
            "plan_digest": fingerprint(items), "unresolved": sum(selected(i, decisions)[0] is None for i in items),
            "items": [dict(i, choice=selected(i, decisions)[0]) for i in items], "bridge": summary}


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
