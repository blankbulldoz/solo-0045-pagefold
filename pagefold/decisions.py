from datetime import datetime, timezone

from .codec import canonical
from .errors import Problem
from .plans import expect_open, expect_revision, get_plan, plan_content


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def next_sequence(db, plan_id):
    row = db.execute("SELECT COALESCE(MAX(seq)+1,0) FROM decision_events WHERE plan_id=?",
                     (plan_id,)).fetchone()
    return row[0]


def resolve(store, plan_id, revision, item_id, choice):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        if not any(item["id"] == item_id and item["automatic"] is None for item in items):
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        new_choice = None if choice == "clear" else choice
        # 同版同值是幂等重试：不推进版序，但仍留下可复核的事件。
        advanced = new_choice != decisions.get(item_id)
        if advanced:
            if new_choice is None:
                decisions.pop(item_id)
            else:
                decisions[item_id] = new_choice
            revision += 1
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, plan_id))
        db.execute(
            "INSERT INTO decision_events(plan_id,seq,revision,item,choice,advanced,recorded_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (plan_id, next_sequence(db, plan_id), revision, item_id, new_choice,
             1 if advanced else 0, stamp()))
        return {"id": plan_id, "revision": revision, "item": item_id, "choice": new_choice,
                "advanced": advanced}
