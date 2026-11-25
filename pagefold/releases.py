import json

from .attempts import get_attempt
from .codec import canonical, digest, fingerprint
from .errors import Problem
from .images import get_image
from .impact import LEGACY_IMPACT, impact_from_candidate
from .plans import expect_open, expect_revision, get_plan, plan_content

CERTIFICATE_FORMAT = "pagefold-certificate-v2"


def get_release(db, plan_id, body=False):
    columns = "certificate,candidate" if body else "certificate"
    row = db.execute(f"SELECT {columns} FROM releases WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unpublished", "该计划还没有封存成果。", 404)
    return row


def present(certificate):
    """读取凭证时补齐影响事实；旧版凭证显式标明未知范围。"""
    if "impact" not in certificate:
        certificate = dict(certificate)
        certificate["impact"] = dict(LEGACY_IMPACT)
        certificate["impact_digest"] = None
    return certificate


def seal(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        if plan["state"] == "sealed":
            return present(json.loads(get_release(db, plan_id)["certificate"]))
        expect_open(plan)
        attempt = get_attempt(db, plan_id, revision)
        if attempt is None or attempt["candidate"] is None:
            raise Problem("rehearsal_required", "当前版本须先通过重演。", 409)
        result = json.loads(attempt["result"])
        if digest(attempt["candidate"]) != result.get("candidate_digest"):
            raise Problem("candidate_digest", "候选摘要不一致，无法封存。", 409)
        items, decisions = plan_content(plan)
        ancestor = get_image(db, plan["base"], body=True)
        catalog = json.loads(ancestor["catalog"])
        # 封存时独立解包候选字节重算影响事实，并与重演记录交叉核对。
        impact = impact_from_candidate(attempt["candidate"], catalog, items, decisions)
        if result.get("status") == "passed" and result.get("impact") != impact:
            raise Problem("impact_mismatch", "重演影响事实与候选数据库不一致，无法封存。", 409)
        certificate = {"format": CERTIFICATE_FORMAT, "plan": plan_id, "revision": revision,
                       "sources": {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
                       "schema": catalog["signature"],
                       "plan_digest": fingerprint(items), "decision_digest": fingerprint(decisions),
                       "candidate_digest": result["candidate_digest"], "candidate_size": result["size"],
                       "impact": impact, "impact_digest": fingerprint(impact)}
        db.execute("INSERT INTO releases VALUES(?,?,?)", (plan_id, canonical(certificate), attempt["candidate"]))
        db.execute("UPDATE plans SET state='sealed' WHERE id=?", (plan_id,))
        return certificate
