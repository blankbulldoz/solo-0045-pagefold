import json
import uuid

from .codec import canonical, fingerprint
from .entities import (annotate_plan_items, build_entities, entity_view,
                       prepare_specs, unresolved_entities)
from .errors import Problem
from .images import get_image
from .merge import ROLES, calculate, selected
from .rows import read_image
from .sqlite_image import mounted


def create_plan(store, sources, options=None, entity_keys=None, entity_specs=None,
                stable_keys=None, natural_keys=None, identity_keys=None, mode=None):
    if options is None:
        options = {}
    if isinstance(options, dict):
        aliases = {
            "entity_keys": entity_keys, "entity_specs": entity_specs,
            "stable_keys": stable_keys, "natural_keys": natural_keys,
            "identity_keys": identity_keys,
        }
        for key, value in aliases.items():
            if value is not None:
                options.setdefault(key, value)
        if mode:
            options.setdefault("mode", mode)
    elif entity_keys is not None and options is None:
        options = entity_keys
    with store.connect() as db:
        images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "三份镜像的模式记录不一致。", 409)
    specs = prepare_specs(catalogs[0], options)
    snapshots = []
    for image, catalog in zip(images, catalogs):
        with mounted(image["body"]) as snapshot:
            snapshots.append(read_image(snapshot, catalog))
    items = calculate(*snapshots)
    entities = build_entities(
        {role: snapshots[index] for index, role in enumerate(ROLES)}, catalogs[0], specs
    ) if specs else []
    if specs:
        annotate_plan_items(items, entities, catalogs[0], specs)
    plan_id = uuid.uuid4().hex
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO plans(id,base,left_id,right_id,items,entity_specs,entities) "
            "VALUES(?,?,?,?,?,?,?)",
            (plan_id, *[sources[role] for role in ROLES], canonical(items),
             canonical(specs), canonical(entities)))
        return read_plan(db, plan_id)


def get_plan(db, plan_id):
    row = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unknown_plan", "找不到指定的合流计划。", 404)
    return row


def plan_content(row):
    return json.loads(row["items"]), json.loads(row["decisions"])


def plan_entities(row):
    try:
        specs = json.loads(row["entity_specs"] or "[]")
        entities = json.loads(row["entities"] or "[]")
    except (IndexError, KeyError):  # pragma: no cover - rows always migrated
        specs, entities = [], []
    return specs, entities


def read_plan(db, plan_id):
    row = get_plan(db, plan_id)
    items, decisions = plan_content(row)
    specs, stored_entities = plan_entities(row)
    entity_views = [entity_view(entity, decisions) for entity in stored_entities]
    spec_tables = {spec["table"] for spec in specs}
    unresolved_entity_ids = set(unresolved_entities(stored_entities, decisions))
    unresolved = len(unresolved_entity_ids)
    for item in items:
        if item["table"] in spec_tables:
            # Entity groups are counted once; raw key variants are not separate.
            continue
        if item.get("reference_driven"):
            # Its uncertainty is already represented by the referenced entity
            # group; do not ask for a second, unrelated row decision.
            continue
        if selected(item, decisions)[0] is None:
            unresolved += 1
    sources = {"base": row["base"], "left": row["left_id"], "right": row["right_id"]}
    entity_decisions = {
        entity["id"]: {
            "choice": entity["choice"], "status": entity["status"],
            "decision": entity["decision"], "accepted": entity["accepted"],
            "rejected": entity["rejected"],
        }
        for entity in entity_views if entity["explicit"]
    }
    return {"id": plan_id, "sources": sources, "revision": row["revision"], "state": row["state"],
            "mode": "entities" if specs else "rows",
            "entity_keys": specs, "entities": entity_views,
            "entity_decisions": entity_decisions, "rename_decisions": entity_decisions,
            "entity_groups": entity_views,
            "rename_groups": [entity for entity in entity_views if entity.get("rename")],
            "renames": [entity for entity in entity_views if entity.get("rename")],
            "plan_digest": fingerprint(items), "unresolved": unresolved,
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
