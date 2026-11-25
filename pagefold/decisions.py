import json

from .codec import canonical
from .errors import Problem
from .plans import expect_open, expect_revision, get_plan, plan_content
from .render import ROLE_LABELS


def conflict_item(items, item_id):
    return next((item for item in items
                 if item["id"] == item_id and item["automatic"] is None), None)


def resolve(store, plan_id, revision, item_id, choice):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        if conflict_item(items, item_id) is None:
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
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


def preview_clear(store, plan_id, revision, item_id):
    """只读地汇总清空一条冲突裁决对当前候选与既有重演证据的影响。"""
    with store.connect() as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        item = conflict_item(items, item_id)
        if item is None:
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        current_choice = decisions.get(item_id)
        if current_choice is None:
            raise Problem("decision_absent", "该冲突尚未裁决，没有可清空的选择。", 409)
        attempts = [dict(row) for row in db.execute(
            "SELECT revision,result,candidate FROM attempts WHERE plan_id=? ORDER BY revision DESC",
            (plan_id,))]
        current = next((attempt for attempt in attempts if attempt["revision"] == revision), None)
        current_passed = bool(current and current["candidate"] is not None)
        history = [attempt for attempt in attempts if attempt["revision"] != revision]
        next_revision = revision + 1
        unresolved_now = sum(
            (entry["automatic"] or decisions.get(entry["id"])) is None for entry in items)
        return {
            "id": plan_id,
            "revision": revision,
            "next_revision": next_revision,
            "item": item,
            "current_choice": current_choice,
            "current_choice_label": ROLE_LABELS[current_choice],
            "current_candidate": {
                "present": current_passed,
                "candidate_digest": json.loads(current["result"]).get("candidate_digest")
                if current_passed else None,
            },
            "rehearsal": {
                "available_now": current_passed,
                "blocked_after_clear": True,
                "reason": "清空后该条目回到未决状态，存在未裁决行时无法重演。",
            },
            "evidence": {
                "history_count": len(history),
                "passed_count": sum(1 for attempt in history if attempt["candidate"] is not None),
                "retained": True,
                "revisions": [attempt["revision"] for attempt in history],
            },
            "after": {
                "choice": None,
                "state": "unresolved",
                "unresolved": unresolved_now + 1,
            },
        }
