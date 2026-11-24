import json

from .attempts import get_attempt
from .codec import canonical, digest, fingerprint
from .errors import Problem
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, plan_content


def get_release(db, plan_id, body=False):
    columns = "certificate,candidate" if body else "certificate"
    row = db.execute(f"SELECT {columns} FROM releases WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unpublished", "该计划还没有封存成果。", 404)
    return row


def seal(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        if plan["state"] == "sealed":
            return json.loads(get_release(db, plan_id)["certificate"])
        expect_open(plan)
        attempt = get_attempt(db, plan_id, revision)
        if attempt is None or attempt["candidate"] is None:
            raise Problem("rehearsal_required", "当前版本须先通过重演。", 409)
        result = json.loads(attempt["result"])
        if digest(attempt["candidate"]) != result.get("candidate_digest"):
            raise Problem("candidate_digest", "候选摘要不一致，无法封存。", 409)
        items, decisions = plan_content(plan)
        amendments = json.loads(plan["amendments"])
        scope = None if plan["scope"] is None else json.loads(plan["scope"])
        certificate = {"format": "pagefold-certificate-v1", "plan": plan_id, "revision": revision,
                       "sources": {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
                       "schema": json.loads(get_image(db, plan["base"])["catalog"])["signature"],
                       "plan_digest": fingerprint(items), "decision_digest": fingerprint(decisions),
                       "scope": scope, "amendments": amendments,
                       "scope_digest": fingerprint({"scope": scope, "amendments": amendments}),
                       "candidate_digest": result["candidate_digest"], "candidate_size": result["size"]}
        db.execute("INSERT INTO releases VALUES(?,?,?)", (plan_id, canonical(certificate), attempt["candidate"]))
        db.execute("UPDATE plans SET state='sealed' WHERE id=?", (plan_id,))
        return certificate
