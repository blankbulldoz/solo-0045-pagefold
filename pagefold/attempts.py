import json
import sqlite3

from .codec import canonical, digest, fingerprint
from .decisions import stamp
from .errors import Problem
from .images import get_image
from .merge import selected
from .plans import expect_open, expect_revision, get_plan, plan_content
from .replay import amendments, rebuild


def get_attempt_row(db, plan_id, revision):
    return db.execute("SELECT * FROM attempts WHERE plan_id=? AND revision=?",
                      (plan_id, revision)).fetchone()


def get_attempt(db, plan_id, revision):
    return get_attempt_row(db, plan_id, revision)


def choice_snapshot(items, decisions):
    """固定重演当时每一项实际生效的裁决。

    自动项与人工冲突项都记录来源角色；被清空（clear）的冲突项没有角色，
    与显式选择祖本（base）区分开，因此最终行相同也能还原当时的操作。
    """
    choices = []
    for item in items:
        role, _ = selected(item, decisions)
        choices.append({"item": item["id"], "table": item["table"], "key": item["key"],
                        "automatic": item["automatic"], "choice": role,
                        "decided": item["id"] in decisions})
    return choices


def render_attempt(row):
    result = json.loads(row["result"])
    if row["choices"] is not None:
        result["record"] = "current"
        result["choices"] = json.loads(row["choices"])
        result["decision_digest"] = row["decision_digest"]
        result["requested_at"] = row["requested_at"]
        result["sequence"] = row["sequence"]
    else:
        # 升级前写入的旧记录：没有选择细节，只能回查重演事实本身。
        result["record"] = "legacy"
        result["choices"] = None
    return result


def list_attempts(db, plan_id):
    get_plan(db, plan_id)
    return [render_attempt(row) for row in db.execute(
        "SELECT * FROM attempts WHERE plan_id=? ORDER BY revision DESC, sequence DESC", (plan_id,))]


def rehearse(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        previous = get_attempt_row(db, plan_id, revision)
        if previous is not None:
            # 同版重演复用固定证据，重演历史不产生第二条互相矛盾的记录。
            return render_attempt(previous)
        items, decisions = plan_content(plan)
        operations = amendments(items, decisions)
        ancestor = get_image(db, plan["base"], body=True)
        choices = choice_snapshot(items, decisions)
        candidate = None
        result = {"id": plan_id, "revision": revision, "changed": sum(map(len, operations.values()))}
        try:
            candidate = rebuild(ancestor["body"], json.loads(ancestor["catalog"]), operations)
            result.update(status="passed", candidate_digest=digest(candidate), size=len(candidate))
        except Problem as exc:
            result.update(status="blocked", problem=exc.as_dict())
        except sqlite3.Error as exc:
            result.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
        db.execute(
            "INSERT INTO attempts(plan_id,revision,result,candidate,choices,decision_digest,"
            "requested_at,sequence) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, revision, canonical(result), candidate, canonical(choices),
             fingerprint(decisions), stamp(),
             db.execute("SELECT COALESCE(MAX(sequence)+1,0) FROM attempts WHERE plan_id=?",
                        (plan_id,)).fetchone()[0]))
        stored = get_attempt_row(db, plan_id, revision)
        return render_attempt(stored)
