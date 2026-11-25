from .codec import canonical
from .entities import normalize_entity_decision
from .errors import Problem
from .inputs import token
from .merge import ROLES
from .plans import expect_open, expect_revision, get_plan, plan_content, plan_entities


def resolve(store, plan_id, revision, item_id, choice):
    # Backwards-compatible entry point: entity ids may also be resolved here.
    with store.connect() as db:
        plan = get_plan(db, plan_id)
        _, entity_groups = plan_entities(plan)
        is_entity = any(entity["id"] == item_id for entity in entity_groups)
    if is_entity:
        return resolve_entity(store, plan_id, revision, item_id, choice)

    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        specs, _ = plan_entities(plan)
        target = next((item for item in items if item["id"] == item_id), None)
        if not target or target["automatic"] is not None:
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        spec_tables = {spec["table"] for spec in specs}
        if target["table"] in spec_tables or target.get("reference_driven"):
            raise Problem("entity_decision_required",
                          "该主键变体或复合外键跟随实体改名组，请使用实体 accept/reject 裁决。", 409)
        new_choice = None if choice == "clear" else choice
        if new_choice != decisions.get(item_id):
            if new_choice is None:
                decisions.pop(item_id)
            else:
                decisions[item_id] = new_choice
            revision += 1
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, plan_id))
        return {"id": plan_id, "revision": revision, "item": item_id, "choice": new_choice}


def _entity_plan(db, plan_id, revision):
    plan = get_plan(db, plan_id)
    expect_revision(plan, revision)
    expect_open(plan)
    _, decisions = plan_content(plan)
    _, entities = plan_entities(plan)
    return plan, decisions, entities


def resolve_entity(store, plan_id, revision, entity_id, choice=None, data=None):
    data = dict(data or {})
    if choice is not None:
        data.setdefault("choice", choice)
    with store.connect(write=True) as db:
        plan, decisions, entities = _entity_plan(db, plan_id, revision)
        group = next((entity for entity in entities if entity["id"] == entity_id), None)
        if group is None:
            raise Problem("unknown_entity", "找不到指定的实体改名组。", 404)
        record = normalize_entity_decision(data, group)
        previous = decisions.get(entity_id)
        changed = previous is not None if record is None else canonical(record) != canonical(previous)
        if changed:
            if record is None:
                decisions.pop(entity_id, None)
            else:
                decisions[entity_id] = record
            revision += 1
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, plan_id))
        response = {"id": plan_id, "revision": revision, "entity": entity_id,
                    "item": entity_id, "choice": record["choice"] if record else None,
                    "clear": record is None,
                    "accepted": bool(record and record["status"] == "accepted"),
                    "rejected": bool(record and record["status"] == "rejected")}
        if record:
            response.update(decision=record["status"], status=record["status"])
        else:
            response.update(decision=None, status=None)
        return response


def resolve_decision(store, plan_id, revision, data):
    entity_id = (data.get("entity") or data.get("entity_id") or data.get("entityId")
                 or data.get("group") or data.get("group_id") or data.get("rename")
                 or data.get("rename_id") or data.get("entity_item"))
    if entity_id:
        token(entity_id, "entity")
        return resolve_entity(store, plan_id, revision, entity_id, data=data)
    item_id = token(data.get("item"), "item")
    choice = data.get("choice")
    if choice not in (*ROLES, "clear"):
        # Accept explicit entity verbs even if the id was supplied as "item".
        verb = str(choice).replace("-", "_").lower() if choice is not None else ""
        if choice in ("accept", "accepted", "reject", "rejected") or verb.startswith(
                ("accept_", "accepted_", "reject", "choose_")):
            return resolve_entity(store, plan_id, revision, item_id, data=data)
        raise Problem("choice", "请选择祖本、左支、右支或清除裁决。", 400)
    return resolve(store, plan_id, revision, item_id, choice)
