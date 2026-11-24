"""移植重演：在新祖本副本上应用移植操作并校验约束，单独留存证据。"""

import json
import sqlite3

from .codec import canonical, digest
from .errors import Problem
from .images import get_image
from .port import (GROUP_COMPLETE, GROUP_LABELS, GROUP_PENDING,
                   mark_blocked_groups)
from .port_plans import get_port, port_content, rebuild_view
from .replay import port_operations, rebuild_port


def get_port_attempt(db, port_id, revision):
    return db.execute("SELECT result,candidate FROM port_attempts WHERE port_id=? AND revision=?",
                      (port_id, revision)).fetchone()


def list_port_attempts(db, port_id):
    get_port(db, port_id)
    return [json.loads(row[0]) for row in db.execute(
        "SELECT result FROM port_attempts WHERE port_id=? ORDER BY revision DESC", (port_id,))]


def rehearse_port(store, port_id, revision):
    with store.connect(write=True) as db:
        row = get_port(db, port_id)
        if row["revision"] != revision:
            raise Problem("stale_revision", "移植计划版本已改变，请刷新后再操作。", 409)
        previous = get_port_attempt(db, port_id, revision)
        if previous:
            return json.loads(previous["result"])
        items, decisions = port_content(row)
        operations = port_operations(items, decisions)
        target = get_image(db, row["target"], body=True)
        catalog = json.loads(target["catalog"])
        result = {"id": port_id, "kind": "port", "revision": revision,
                  "planned": sum(map(len, operations.values()))}
        candidate = None
        try:
            candidate, applied, skipped = rebuild_port(target["body"], catalog, operations)
            result.update(status="passed", candidate_digest=digest(candidate),
                          size=len(candidate), applied=applied, skipped=skipped)
        except Problem as exc:
            result.update(status="blocked", applied=0, skipped=0, problem=exc.as_dict())
        except sqlite3.Error as exc:
            result.update(status="blocked", applied=0, skipped=0,
                          problem={"code": "sqlite_replay", "detail": str(exc)})
        # 改名组完整性：受阻时把待落地的改名降级，绝不把半套动作报成完整移植
        view = rebuild_view(items, decisions, catalog)
        if result["status"] == "blocked":
            mark_blocked_groups(view, result["problem"]["code"])
        result["rename_groups"] = [
            {"id": g["id"], "table": g["table"], "status": g["status"],
             "status_label": GROUP_LABELS[g["status"]],
             "old_key": g["old_key"], "new_key": g["new_key"],
             "blocked_by": g.get("blocked_by")}
            for g in view["groups"]]
        result["rename_complete"] = all(g["status"] == GROUP_COMPLETE for g in view["groups"])
        result["rename_pending"] = any(g["status"] == GROUP_PENDING for g in view["groups"])
        db.execute("INSERT INTO port_attempts VALUES(?,?,?,?)",
                   (port_id, revision, canonical(result), candidate))
        return result
