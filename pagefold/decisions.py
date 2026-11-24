import json

from .codec import canonical
from .errors import Problem
from .plans import expect_open, expect_revision, get_plan, plan_content


def history(db, plan_id):
    return [{"revision": row["revision"], "item": row["item"],
             "previous": json.loads(row["previous"]) if row["previous"] else None,
             "choice": json.loads(row["choice"]) if row["choice"] else None,
             "origin": row["origin"], "actor": row["actor"]}
            for row in db.execute("SELECT revision,item,previous,choice,origin,actor"
                                  " FROM decision_log WHERE plan_id=? ORDER BY revision,item",
                                  (plan_id,))]


def record(db, plan_id, revision, changes, origin, actor=None):
    for item_id, previous, choice in changes:
        db.execute("INSERT OR REPLACE INTO decision_log(plan_id,revision,item,previous,choice,origin,actor)"
                   " VALUES(?,?,?,?,?,?,?)",
                   (plan_id, revision, item_id, json.dumps(previous) if previous else None,
                    json.dumps(choice) if choice else None, origin, actor))


def resolve(store, plan_id, revision, item_id, choice, actor=None):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        if not any(item["id"] == item_id and item["automatic"] is None for item in items):
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        new_choice = None if choice == "clear" else choice
        previous = decisions.get(item_id)
        if new_choice != previous:
            if new_choice is None:
                decisions.pop(item_id, None)
            else:
                decisions[item_id] = new_choice
            revision += 1
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, plan_id))
            record(db, plan_id, revision, [(item_id, previous, new_choice)], "direct", actor)
        return {"id": plan_id, "revision": revision, "item": item_id, "choice": new_choice}
