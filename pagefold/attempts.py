import json
import sqlite3

from .codec import canonical, digest
from .errors import Problem
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, plan_content, scope_report
from .replay import rebuild
from .scope import operations as scope_operations


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
        base = get_image(db, plan["base"], body=True)
        report = scope_report(plan, base)
        if any(entry["choice"] is None for entry in report["active"]):
            raise Problem("unresolved", "范围内存在未裁决的行，请先完成裁决。", 409)
        if not report["full_schema"]:
            if report["blocked"]:
                first = report["blocked"][0]
                raise Problem("scope_blocked",
                              f"当前关系集合无法保持约束：{first['detail']}（建议{first['remedy']['action']} "
                              f"{'、'.join(first['remedy']['tables'])}，共 {len(report['blocked'])} 项阻塞）", 409)
            if report["pending_derived"]:
                raise Problem("scope_amendment",
                              f"存在 {len(report['pending_derived'])} 项维持约束所需的范围外派生变更，请先确认。", 409)
        operations = scope_operations(report, items, decisions)
        result = {"id": plan_id, "revision": revision,
                  "changed": sum(map(len, operations.values())),
                  "active_changed": report["active_change_count"],
                  "derived_changed": len(report["confirmed"]),
                  "untouched_tables": report["untouched_tables"],
                  "scope": report["scope"]}
        candidate = None
        try:
            candidate = rebuild(base["body"], json.loads(base["catalog"]), operations)
            result.update(status="passed", candidate_digest=digest(candidate), size=len(candidate))
        except Problem as exc:
            result.update(status="blocked", problem=exc.as_dict())
        except sqlite3.Error as exc:
            result.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
        db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (plan_id, revision, canonical(result), candidate))
        return result
