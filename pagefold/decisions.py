import json

from .codec import canonical
from .errors import Problem
from .images import get_image
from .manual import build_manual_row, decision_envelope, get_table, normalize_key_ref
from .plans import expect_open, expect_revision, get_plan, plan_content

CHOICES = ("base", "left", "right", "manual", "clear")


def _conflict_item(items, item_ref):
    if isinstance(item_ref, dict):
        table = item_ref.get("table")
        try:
            key = normalize_key_ref(item_ref.get("key"))
        except Problem:
            key = item_ref.get("key")
        match = next((candidate for candidate in items
                      if candidate["automatic"] is None and candidate["table"] == table
                      and candidate["key"] == key), None)
        if match is None:
            raise Problem("not_conflict", "该表/主键找不到可裁决的冲突条目。", 409,
                          {"stage": "decision", "item": item_ref})
        return match
    item = next((candidate for candidate in items if candidate["id"] == item_ref), None)
    if item is None or item["automatic"] is not None:
        raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409,
                      {"stage": "decision", "item": item_ref})
    return item


def resolve(store, plan_id, revision, item_ref, choice, payload=None):
    payload = payload or {}
    choice, payload = decision_envelope(choice, payload)
    if choice not in CHOICES:
        raise Problem("choice", "请选择祖本、左支、右支、人工合成或清除裁决。", 400,
                      {"stage": "decision_input", "item": item_ref})
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        try:
            expect_revision(plan, revision)
        except Problem as exc:
            exc.info.update({"stage": "decision", "item": item_ref,
                            "submitted_revision": revision, "current_revision": plan["revision"],
                            "choice": "manual" if isinstance(choice, dict) else choice})
            raise
        expect_open(plan)
        items, decisions = plan_content(plan)
        item = _conflict_item(items, item_ref)
        item_id = item["id"]

        if choice == "clear":
            new_decision = None
        elif choice == "manual":
            catalog = json.loads(get_image(db, plan["base"])["catalog"])
            table = get_table(catalog, item["table"])
            row = build_manual_row(payload, item, table, item_id)
            new_decision = {"choice": "manual", "row": row}
        else:
            new_decision = choice

        if new_decision != decisions.get(item_id):
            if new_decision is None:
                decisions.pop(item_id, None)
            else:
                decisions[item_id] = new_decision
            revision = plan["revision"] + 1
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, plan_id))
        else:
            revision = plan["revision"]
        result = {"id": plan_id, "revision": revision, "item": item_id,
                  "choice": None if new_decision is None else choice}
        if isinstance(new_decision, dict):
            result["row"] = new_decision["row"]
            result["decision"] = new_decision
        return result
