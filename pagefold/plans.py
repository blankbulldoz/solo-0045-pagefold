import json
import uuid

from .codec import canonical, fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES, calculate, selected
from .rows import read_image
from .sqlite_image import mounted


def prepare(store, sources):
    """Load and validate the three sources and compute the three-way items.

    Nothing is written here, so callers can show a preview without leaving a
    plan record behind.  Image ids are byte digests and therefore pin the
    exact content for every role.
    """
    with store.connect() as db:
        images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    return images, calculate(*snapshots)


def insert_plan(db, sources, items):
    plan_id = uuid.uuid4().hex
    db.execute("INSERT INTO plans(id,base,left_id,right_id,items) VALUES(?,?,?,?,?)",
               (plan_id, *[sources[role] for role in ROLES], canonical(items)))
    return plan_id


def create_plan(store, sources):
    _images, items = prepare(store, sources)
    with store.connect(write=True) as db:
        plan_id = insert_plan(db, sources, items)
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
    return {"id": plan_id, "sources": sources, "revision": row["revision"], "state": row["state"],
            "plan_digest": fingerprint(items), "unresolved": sum(selected(i, decisions)[0] is None for i in items),
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
