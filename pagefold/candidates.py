import json

from .attempts import get_attempt
from .codec import canonical
from .errors import Problem
from .images import get_image
from .merge import selected
from .plans import get_plan, plan_content
from .rows import read_image, read_table
from .sqlite_image import mounted


def read_attempt(db, plan_id, revision):
    plan = get_plan(db, plan_id)
    attempt = get_attempt(db, plan_id, revision)
    if attempt is None:
        raise Problem("unknown_attempt", "找不到该版的重演记录。", 404)
    return plan, json.loads(attempt["result"]), attempt["candidate"]


def require_candidate(db, plan_id, revision):
    plan, result, candidate = read_attempt(db, plan_id, revision)
    if candidate is None:
        raise Problem("candidate_unavailable", "该版重演受阻，没有可检查的候选。", 409)
    return plan, result, candidate


def catalog_of(db, plan):
    return json.loads(get_image(db, plan["base"])["catalog"])


def candidate_record(db, plan_id, revision):
    plan, result, candidate = read_attempt(db, plan_id, revision)
    record = {"id": plan_id, "revision": result["revision"], "status": result["status"],
              "changed": result["changed"], "current": plan["revision"] == result["revision"],
              "state": plan["state"]}
    if candidate is None:
        record["problem"] = result["problem"]
        return record
    record["candidate_digest"] = result["candidate_digest"]
    record["size"] = result["size"]
    catalog = catalog_of(db, plan)
    with mounted(candidate) as snapshot:
        rows = read_image(snapshot, catalog)
    record["tables"] = [{"name": table["name"], "key": table["key"],
                         "rows": len(rows[table["name"]])} for table in catalog["tables"]]
    return record


def candidate_overview(db, plan_id, revision):
    plan, result, candidate = read_attempt(db, plan_id, revision)
    overview = {"plan": plan, "result": result, "tables": None, "entries": None}
    if candidate is None:
        return overview
    catalog = catalog_of(db, plan)
    with mounted(candidate) as snapshot:
        rows = read_image(snapshot, catalog)
    overview["tables"] = [dict(table, rows=len(rows[table["name"]])) for table in catalog["tables"]]
    items, decisions = plan_content(plan)
    overview["entries"] = [dict(item, choice=selected(item, decisions)[0],
                                candidate=rows[item["table"]].get(canonical(item["key"])))
                           for item in items]
    return overview


def candidate_rows(db, plan_id, revision, name, offset):
    plan, _, candidate = require_candidate(db, plan_id, revision)
    catalog = catalog_of(db, plan)
    table = next((table for table in catalog["tables"] if table["name"] == name), None)
    if table is None:
        raise Problem("unknown_table", "候选中没有该表。", 404)
    with mounted(candidate) as snapshot:
        rows = read_table(snapshot, table)
    keys = sorted(rows)
    return {"table": table, "offset": offset, "total": len(keys),
            "rows": [{"key": json.loads(key), "values": rows[key]} for key in keys[offset:offset + 50]]}
