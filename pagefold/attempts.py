import json
import sqlite3

from .codec import canonical, digest
from .errors import Problem
from .images import get_image, image_catalog
from .plans import expect_open, expect_revision, get_plan, plan_content
from .replay import amendments, rebuild


def get_attempt(db, plan_id, revision):
    return db.execute("SELECT result,candidate FROM attempts WHERE plan_id=? AND revision=?",
                      (plan_id, revision)).fetchone()


def list_attempts(db, plan_id):
    get_plan(db, plan_id)
    return [json.loads(row[0]) for row in db.execute(
        "SELECT result FROM attempts WHERE plan_id=? ORDER BY revision DESC", (plan_id,))]


def rehearse(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        previous = get_attempt(db, plan_id, revision)
        if previous:
            return json.loads(previous["result"])
        items, decisions = plan_content(plan)
        operations = amendments(items, decisions)
        ancestor = get_image(db, plan["base"], body=True)
        candidate = None
        result = {"id": plan_id, "revision": revision, "changed": sum(map(len, operations.values()))}
        try:
            candidate = rebuild(ancestor["body"], image_catalog(ancestor), operations)
            result.update(status="passed", candidate_digest=digest(candidate), size=len(candidate))
        except Problem as exc:
            result.update(status="blocked", problem=exc.as_dict())
        except sqlite3.Error as exc:
            result.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
        db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (plan_id, revision, canonical(result), candidate))
        return result
