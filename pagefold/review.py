import json
import re
import sqlite3

from .codec import atom, canonical, digest, fingerprint
from .errors import Problem
from .images import get_image
from .plans import get_plan
from .releases import get_release
from .sqlite_image import mounted

REVIEW_ROLES = ("base", "left", "right", "result")
MAX_SQL = 8_000
MAX_ROWS = 20_000
MAX_RESULT_BYTES = 16_000_000
STEP_BUDGET = 4_000_000

LEADING = re.compile(r"(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)
WORD = re.compile(r"[A-Za-z]+")
READONLY = {"SELECT", "WITH", "VALUES"}


def check_query(sql):
    if not isinstance(sql, str) or not sql.strip():
        raise Problem("sql", "请提供要复核的 SQL 查询。", 400)
    if len(sql) > MAX_SQL:
        raise Problem("sql_size", "查询文本超过 8,000 字符上限。", 413)
    word = WORD.match(sql, LEADING.match(sql).end())
    if word is None or word.group(0).upper() not in READONLY:
        raise Problem("query_kind", "复核仅接受 SELECT、WITH 或 VALUES 只读查询。", 422)


def sealed_plan(db, plan_id):
    plan = get_plan(db, plan_id)
    if plan["state"] not in ("sealed", "archived"):
        raise Problem("not_sealed", "计划封存后才能运行查询复核。", 409)
    return plan


def budget_guard(db):
    state = {"spent": 0}

    def handler():
        state["spent"] += 1000
        return 1 if state["spent"] > STEP_BUDGET else 0

    db.set_progress_handler(handler, 1000)


def run_query(body, sql):
    with mounted(body) as db:
        budget_guard(db)
        cursor = db.execute(sql)
        columns = [field[0] for field in cursor.description or ()]
        tokens, size = [], 0
        for raw in cursor:
            if len(tokens) >= MAX_ROWS:
                raise Problem("row_limit", "查询结果超过 20,000 行上限。")
            token = canonical([atom(value) for value in raw])
            size += len(token)
            if size > MAX_RESULT_BYTES:
                raise Problem("result_size", "查询结果超出 16 MiB 复核容量。")
            tokens.append(token)
    tokens.sort()
    return {"columns": columns, "rows": [json.loads(token) for token in tokens],
            "row_count": len(tokens), "digest": fingerprint([columns, tokens])}


def role_run(role, source, body, sql):
    try:
        outcome = run_query(body, sql)
    except Problem as exc:
        return {"role": role, "source": source, "status": "error", "error": exc.as_dict()}
    except sqlite3.Error as exc:
        detail, code = str(exc), "query_failed"
        if "interrupted" in detail:
            code, detail = "query_cost", "查询超出复核的计算预算。"
        elif "readonly" in detail:
            code, detail = "query_write", "复核仅接受只读查询。"
        return {"role": role, "source": source, "status": "error",
                "error": {"code": code, "detail": detail}}
    return {"role": role, "source": source, "status": "ok", **outcome}


def compare(roles):
    outcomes = {role["role"]: role for role in roles}
    verdicts = []
    for index, first in enumerate(REVIEW_ROLES):
        for second in REVIEW_ROLES[index + 1:]:
            pair = [outcomes[first], outcomes[second]]
            if all(role["status"] == "ok" for role in pair):
                verdict = "equal" if pair[0]["digest"] == pair[1]["digest"] else "different"
            else:
                verdict = "incomparable"
            verdicts.append({"pair": [first, second], "verdict": verdict})
    return verdicts


def run_review(db, plan_id, sql):
    check_query(sql)
    plan = sealed_plan(db, plan_id)
    bodies = {role: get_image(db, plan[key], body=True)["body"]
              for role, key in (("base", "base"), ("left", "left_id"), ("right", "right_id"))}
    bodies["result"] = get_release(db, plan_id, body=True)["candidate"]
    sources = {role: digest(body) for role, body in bodies.items()}
    roles = [role_run(role, sources[role], bodies[role], sql) for role in REVIEW_ROLES]
    return {"plan": plan_id, "state": plan["state"], "revision": plan["revision"], "sql": sql,
            "sql_digest": digest(sql.encode("utf-8")),
            "roles": roles, "comparisons": compare(roles)}
