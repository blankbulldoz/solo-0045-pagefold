import json
import sqlite3

from .codec import canonical, digest
from .errors import Problem
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, plan_content, plan_entities
from .replay import build_changes, rebuild


def get_attempt(db, plan_id, revision):
    return db.execute("SELECT result,candidate FROM attempts WHERE plan_id=? AND revision=?",
                      (plan_id, revision)).fetchone()


def list_attempts(db, plan_id):
    get_plan(db, plan_id)
    return [json.loads(row[0]) for row in db.execute(
        "SELECT result FROM attempts WHERE plan_id=? ORDER BY revision DESC", (plan_id,))]


def rehearse(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        previous = get_attempt(db, plan_id, revision)
        if previous:
            return json.loads(previous["result"])
        items, decisions = plan_content(plan)
        entity_specs, entity_groups = plan_entities(plan)
        ancestor = get_image(db, plan["base"], body=True)
        catalog = json.loads(ancestor["catalog"])
        operations, key_mappings = build_changes(
            items, decisions, catalog, entity_specs, entity_groups)
        candidate = None
        replay_report = {}
        result = {"id": plan_id, "revision": revision,
                  "changed": sum(map(len, operations.values())),
                  "entity_mode": bool(entity_specs)}
        try:
            candidate = rebuild(ancestor["body"], catalog, operations,
                                key_mappings, replay_report)
            reference_updates = replay_report.get("reference_updates", [])
            reference_updates = list(reference_updates)
            for table_operations in operations.values():
                for operation_item, row in table_operations:
                    if operation_item.get("reference_resolved") and row is not None:
                        reference_updates.append({
                            "table": operation_item["table"], "key": operation_item["key"],
                            "resolved_by": "entity_decision"})
            result.update({
                "status": "passed", "candidate_digest": digest(candidate), "size": len(candidate),
                "reference_rows_updated": len(reference_updates),
                "reference_updates": reference_updates,
            })
        except Problem as exc:
            result.update(status="blocked", problem=exc.as_dict())
        except sqlite3.Error as exc:
            result.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
        db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (plan_id, revision, canonical(result), candidate))
        return result
