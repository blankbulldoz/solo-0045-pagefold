import json

from .codec import canonical, fingerprint
from .errors import Problem
from .inputs import token
from .merge import ROLES, selected
from .plans import expect_open, expect_revision, get_plan, plan_content


MAX_ENTRIES = 4096


def resolve(store, plan_id, revision, item_id, choice):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        if not any(item["id"] == item_id and item["automatic"] is None for item in items):
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


def _normalize(entries):
    if not isinstance(entries, list) or not entries:
        raise Problem("entries", "请至少提交一个裁决条目。", 400)
    if len(entries) > MAX_ENTRIES:
        raise Problem("entries", "单次提交的裁决条目过多。", 413)
    choices = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise Problem("entries", "每个裁决条目须包含 item 和 choice。", 400)
        item_id = token(entry.get("item"), "item")
        choice = entry.get("choice")
        if choice not in (*ROLES, "clear"):
            raise Problem("choice", "请选择祖本、左支、右支或清除裁决。", 400)
        if item_id in choices and choices[item_id] != choice:
            raise Problem("duplicate_item", "同一请求对同一条目给出了不同选择。", 409)
        choices[item_id] = choice
    return [(item_id, choices[item_id]) for item_id in sorted(choices)]


def _confirmation(plan_id, revision, entries):
    return fingerprint({"plan": plan_id, "revision": revision,
                        "entries": [{"item": item, "choice": choice} for item, choice in entries]})


def _prepare(normalized, items, decisions):
    index = {item["id"]: item for item in items}
    updated = dict(decisions)
    changes, unchanged = [], []
    for item_id, choice in normalized:
        item = index.get(item_id)
        if item is None:
            raise Problem("unknown_item", "该条目不属于当前计划或已过期。", 409)
        if item["automatic"] is not None:
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        current = decisions.get(item_id)
        new_choice = None if choice == "clear" else choice
        entry = {"item": item_id, "table": item["table"], "key": item["key"]}
        if new_choice == current:
            unchanged.append(dict(entry, choice=new_choice))
            continue
        changes.append(dict(entry, **{"from": current, "to": new_choice}))
        if new_choice is None:
            updated.pop(item_id, None)
        else:
            updated[item_id] = new_choice
    before = sum(selected(item, decisions)[0] is None for item in items)
    after = sum(selected(item, updated)[0] is None for item in items)
    return updated, changes, unchanged, before, after


def preview(store, plan_id, revision, entries):
    with store.connect() as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        normalized = _normalize(entries)
        _, changes, unchanged, before, after = _prepare(normalized, items, decisions)
        return {"id": plan_id, "revision": revision,
                "confirmation": _confirmation(plan_id, revision, normalized),
                "changed": len(changes), "changes": changes, "unchanged": unchanged,
                "next_revision": revision + 1 if changes else revision,
                "unresolved_before": before, "unresolved_after": after}


def resolve_batch(store, plan_id, revision, entries, confirmation=None):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        normalized = _normalize(entries)
        expected = _confirmation(plan_id, revision, normalized)
        if confirmation is not None:
            confirmation = token(confirmation, "confirmation")
            if confirmation != expected:
                raise Problem("confirmation", "确认摘要与本次条目或版本不一致，请重新预览。", 409)
        # A receipt belongs to a batch that was already applied. Replaying the
        # same confirmation after a dropped response reproduces that outcome
        # without writing anything, even if the plan has advanced since then.
        receipt = db.execute("SELECT result FROM decision_receipts WHERE plan_id=? AND confirmation=?",
                             (plan_id, expected)).fetchone()
        if receipt is not None:
            return dict(json.loads(receipt["result"]), replayed=True, current_revision=plan["revision"])
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        updated, changes, unchanged, before, after = _prepare(normalized, items, decisions)
        result_revision = revision + 1 if changes else revision
        if changes:
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(updated), result_revision, plan_id))
        result = {"id": plan_id, "base_revision": revision, "revision": result_revision,
                  "confirmation": expected, "changed": len(changes), "changes": changes,
                  "unchanged": unchanged, "unresolved_before": before, "unresolved_after": after}
        db.execute("INSERT INTO decision_receipts VALUES(?,?,?,?)",
                   (plan_id, expected, result_revision, canonical(result)))
        return dict(result, replayed=False, current_revision=result_revision)
