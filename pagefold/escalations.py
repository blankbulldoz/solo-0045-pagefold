import json

from .attempts import get_attempt
from .codec import canonical
from .errors import Problem
from .plans import expect_open, expect_revision, get_plan, plan_content

MAX_REASON = 200


def escalate(store, plan_id, revision, item_id, reason):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        attempt = get_attempt(db, plan_id, revision)
        if attempt is None or json.loads(attempt["result"]).get("status") != "blocked":
            raise Problem("not_blocked", "仅当前版本重演受阻时才能把自动项转为待复核。", 409)
        items, _, escalations = plan_content(plan)
        item = next((entry for entry in items if entry["id"] == item_id), None)
        if item is None or item["automatic"] is None:
            raise Problem("not_automatic", "该条目不是明确的自动项。", 409)
        if item_id in escalations:
            raise Problem("already_escalated", "该条目已转为待复核。", 409)
        revision += 1
        escalations[item_id] = {"reason": reason, "revision": revision, "automatic": item["automatic"]}
        db.execute("UPDATE plans SET escalations=?,revision=? WHERE id=?",
                   (canonical(escalations), revision, plan_id))
        return {"id": plan_id, "revision": revision, "item": item_id,
                "escalation": escalations[item_id]}
