import json

from .attempts import get_attempt
from .codec import canonical, digest, fingerprint
from .errors import Problem
from .images import get_image
from .inputs import plan_token, unsigned
from .plans import get_plan, plan_content

MIN_MEMBERS, MAX_MEMBERS = 2, 8


def member_descriptor(db, plan_id, revision):
    plan = get_plan(db, plan_id)
    if plan["revision"] != revision:
        raise Problem("revision_drift",
                      f"计划 {plan_id} 当前为第 {plan['revision']} 版，明确选择的第 {revision} 版已漂移，"
                      "整组交付取消。", 409)
    attempt = get_attempt(db, plan_id, revision)
    if attempt is None or attempt["candidate"] is None:
        raise Problem("candidate_missing",
                      f"计划 {plan_id} 第 {revision} 版没有通过重演的合格候选，整组交付取消。", 409)
    result = json.loads(attempt["result"])
    if result.get("candidate_digest") != digest(attempt["candidate"]):
        raise Problem("candidate_digest", "候选摘要不一致，整组交付取消。", 409)
    items, decisions = plan_content(plan)
    return {"plan": plan_id, "revision": revision,
            "sources": {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
            "schema": json.loads(get_image(db, plan["base"])["catalog"])["signature"],
            "plan_digest": fingerprint(items), "decision_digest": fingerprint(decisions),
            "candidate_digest": result["candidate_digest"], "candidate_size": result["size"]}


def create_bundle(store, selection):
    if not isinstance(selection, list) or not MIN_MEMBERS <= len(selection) <= MAX_MEMBERS:
        raise Problem("members", f"联合交付须明确选择 {MIN_MEMBERS} 至 {MAX_MEMBERS} 份计划的版本。", 400)
    pairs = []
    for entry in selection:
        if not isinstance(entry, dict):
            raise Problem("members", "交付成员须为包含 plan 与 revision 的对象。", 400)
        pairs.append((plan_token(entry.get("plan")), unsigned(entry.get("revision"), "revision")))
    if len({plan for plan, _ in pairs}) != len(pairs):
        raise Problem("members", "同一计划不能在同一套交付中重复出现。", 409)
    with store.connect(write=True) as db:
        members = [member_descriptor(db, plan, revision) for plan, revision in sorted(pairs)]
        bundle_id = fingerprint(members)
        manifest = {"format": "pagefold-bundle-v1", "id": bundle_id, "members": members}
        created = db.execute("SELECT 1 FROM bundles WHERE id=?", (bundle_id,)).fetchone() is None
        if created:
            db.execute("INSERT INTO bundles VALUES(?,?)", (bundle_id, canonical(manifest)))
        return manifest, created


def get_bundle(db, bundle_id):
    row = db.execute("SELECT manifest FROM bundles WHERE id=?", (bundle_id,)).fetchone()
    if row is None:
        raise Problem("unknown_bundle", "找不到指定的联合交付。", 404)
    return json.loads(row["manifest"])


def list_bundles(db):
    return [{"id": row["id"], "members": len(json.loads(row["manifest"])["members"])}
            for row in db.execute("SELECT id,manifest FROM bundles ORDER BY rowid DESC")]


def member_body(db, bundle_id, plan_id):
    manifest = get_bundle(db, bundle_id)
    member = next((m for m in manifest["members"] if m["plan"] == plan_id), None)
    if member is None:
        raise Problem("unknown_member", "该计划不在这一套交付中。", 404)
    attempt = get_attempt(db, plan_id, member["revision"])
    if attempt is None or attempt["candidate"] is None or \
            digest(attempt["candidate"]) != member["candidate_digest"]:
        raise Problem("candidate_digest", "交付成员的内容与清单摘要不一致。", 409)
    return attempt["candidate"]


def bundle_view(db, bundle_id):
    manifest = get_bundle(db, bundle_id)
    members = []
    for member in manifest["members"]:
        row = db.execute("""
            SELECT p.revision, p.state, l.label AS left_label, r.label AS right_label,
                   EXISTS(SELECT 1 FROM releases s WHERE s.plan_id=p.id) AS sealed
            FROM plans p JOIN images l ON l.id=p.left_id JOIN images r ON r.id=p.right_id
            WHERE p.id=?""", (member["plan"],)).fetchone()
        members.append(dict(member, current=row["revision"], state=row["state"],
                            sealed=bool(row["sealed"]),
                            label=f'{row["left_label"]} ＋ {row["right_label"]}'))
    return {"manifest": manifest, "members": members}
