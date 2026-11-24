from .codec import canonical
from .errors import Problem
from .plans import (annotate, continuation, expect_open, expect_revision, get_plan, paged,
                    plan_content)


def resolve(store, plan_id, revision, item_id, choice, view=None):
    context = view or {}
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        try:
            expect_revision(plan, revision)
        except Problem as exc:
            if context:
                exc.return_to = paged(plan_id, table=context.get("table"),
                                      pending=context.get("pending", False),
                                      limit=context.get("limit"), revision=plan["revision"])
            raise
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
        result = {"id": plan_id, "revision": revision, "item": item_id, "choice": new_choice}
        if context:
            view = {"table": context.get("table"), "pending": context.get("pending", False),
                    "limit": context.get("limit"), "cursor": context.get("cursor")}
            result["view"] = view
            result["saved"] = {"item": item_id, "choice": new_choice,
                               "pending": next(item["pending"] for item in annotate(items, decisions)
                                               if item["id"] == item_id),
                               "next": continuation(db, plan_id, view, item_id)}
        return result
