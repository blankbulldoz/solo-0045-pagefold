import json

from .attempts import get_attempt_row, render_attempt
from .codec import fingerprint
from .errors import Problem
from .plans import get_plan, plan_content
from .render import ROLE_LABELS

# 重演状态和人工裁决的展示文案。
STATUS_LABELS = {"passed": "通过", "blocked": "受阻"}
CHOICE_LABELS = {**ROLE_LABELS, None: "已清空（保留未决）"}


def list_events(db, plan_id):
    return [dict(row) for row in db.execute(
        "SELECT seq,revision,item,choice,advanced,recorded_at FROM decision_events "
        "WHERE plan_id=? ORDER BY seq", (plan_id,))]


def attempt_detail(db, plan_id, revision):
    row = get_attempt_row(db, plan_id, revision)
    if row is None:
        raise Problem("unknown_attempt", "找不到该版本的重演记录。", 404)
    attempt = render_attempt(row)
    plan = get_plan(db, plan_id)
    items, _ = plan_content(plan)
    item_index = {item["id"]: item for item in items}
    if attempt["choices"] is not None:
        for entry in attempt["choices"]:
            item = item_index.get(entry["item"])
            entry["table"] = item["table"] if item else entry["table"]
            entry["key"] = item["key"] if item else entry["key"]
    return attempt


def dossier(db, plan_id):
    """组装一次样例变更的案卷索引，归档后仍按只读方式完整可查。"""
    plan = get_plan(db, plan_id)
    items, decisions = plan_content(plan)
    attempts = [render_attempt(row) for row in db.execute(
        "SELECT * FROM attempts WHERE plan_id=? ORDER BY revision DESC, sequence DESC", (plan_id,))]
    events = list_events(db, plan_id)
    release_row = db.execute("SELECT certificate FROM releases WHERE plan_id=?", (plan_id,)).fetchone()
    release = json.loads(release_row["certificate"]) if release_row else None
    current = next((a for a in attempts if a["revision"] == plan["revision"]), None)
    legacy = [a for a in attempts if a["record"] == "legacy"]
    return {
        "id": plan_id,
        "state": plan["state"],
        "revision": plan["revision"],
        "sources": {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
        "plan_digest": fingerprint(items),
        "current_decisions": decisions,
        "current_attempt": current,
        "attempts": attempts,
        "events": events,
        "legacy_count": len(legacy),
        "release": release,
        "archived": plan["state"] == "archived",
    }
