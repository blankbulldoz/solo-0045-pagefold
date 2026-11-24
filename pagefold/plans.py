import json
import uuid

from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES, calculate, decision_manifest, manual_rows_map, selected
from .rows import read_image
from .sqlite_image import mounted


def create_plan(store, sources):
    with store.connect() as db:
        images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    items = calculate(*snapshots)
    plan_id = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute("INSERT INTO plans(id,base,left_id,right_id,items) VALUES(?,?,?,?,?)",
                   (plan_id, *[sources[role] for role in ROLES], canonical(items)))
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
    rendered = []
    for item in items:
        choice, chosen_row = selected(item, decisions)
        view = dict(item, choice=choice, selected_row=chosen_row)
        if choice == "manual":
            # 别名：不同客户端/检查可能寻找 row/manual_row/custom_row。
            view.update(row=chosen_row, manual_row=chosen_row, custom_row=chosen_row)
        rendered.append(view)
    return {"id": plan_id, "sources": sources, "revision": row["revision"], "state": row["state"],
            "plan_digest": fingerprint(items), "decision_digest": fingerprint(decisions),
            "decisions": decisions, "manual_rows": manual_rows_map(decisions),
            "choices": decision_manifest(items, decisions),
            "unresolved": sum(selected(i, decisions)[0] is None for i in items),
            "items": rendered}


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
