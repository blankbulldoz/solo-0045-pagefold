from .codec import canonical
from .errors import Problem
from .plans import expect_open, expect_revision, get_plan, plan_content


def resolve(store, plan_id, revision, item_id, choice):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions, escalations = plan_content(plan)
        decidable = any(item["id"] == item_id and
                        (item["automatic"] is None or item_id in escalations) for item in items)
        if not decidable:
            raise Problem("not_conflict", "该条目不是可裁决的冲突或待复核项。", 409)
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
