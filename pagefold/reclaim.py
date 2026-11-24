import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .codec import canonical, fingerprint
from .errors import Problem

FORMAT = "pagefold-reclaim-v1"
ROLE_COLUMNS = {"base": "base", "left": "left_id", "right": "right_id"}


def _reference_holders(db):
    """Map each image to every plan/role that points at it.

    A source is protected while it is still needed by an open plan (which may
    still be rehearsed), a sealed result, or the certificate review of an
    archived result. Archived plans without a release keep their stored items
    and decisions, but no longer need the source bytes.
    """
    released = {row[0] for row in db.execute("SELECT plan_id FROM releases")}
    holders = defaultdict(list)
    for plan in db.execute("SELECT id,state,base,left_id,right_id FROM plans ORDER BY id"):
        sealed = plan["id"] in released
        if plan["state"] == "open":
            reason = "open_plan"
        elif plan["state"] == "sealed":
            reason = "sealed_result"
        elif sealed:
            reason = "certificate_review"
        else:
            reason = None
        for role, column in ROLE_COLUMNS.items():
            holders[plan[column]].append({
                "plan": plan["id"], "state": plan["state"], "sealed": sealed,
                "role": role, "reason": reason})
    return holders


def _snapshot(db):
    """Everything that can change whether a previewed image may still be reclaimed.

    Any new image (even one nobody references yet), re-registration, deletion,
    or new plan reference changes this snapshot, so an old preview cannot be
    used to remove bytes the administrator never saw.
    """
    images = [[row["id"], row["size"], row["reclaimed"]]
              for row in db.execute(
                  "SELECT id,size,reclaimed FROM images ORDER BY id")]
    plans = [[row["id"], row["state"], row["sealed"], row["base"], row["left_id"], row["right_id"]]
             for row in db.execute("""
                 SELECT p.id,p.state,EXISTS(SELECT 1 FROM releases r WHERE r.plan_id=p.id) AS sealed,
                        p.base,p.left_id,p.right_id
                 FROM plans p ORDER BY p.id
             """)]
    return {"format": FORMAT, "images": images, "plans": plans}


def report(db):
    holders = _reference_holders(db)
    retained, reclaimable, reclaimed = [], [], []
    for row in db.execute("SELECT id,label,size,reclaimed FROM images ORDER BY id"):
        references = holders.get(row["id"], [])
        reasons = sorted({ref["reason"] for ref in references if ref["reason"]})
        entry = {"id": row["id"], "label": row["label"], "size": row["size"],
                 "references": references}
        if row["reclaimed"]:
            reclaimed.append(entry)
        elif reasons:
            retained.append(dict(entry, reasons=reasons))
        else:
            reclaimable.append(entry)
    total = sum(entry["size"] for entry in reclaimable)
    retained_total = sum(entry["size"] for entry in retained)
    snapshot = _snapshot(db)
    return {
        "format": FORMAT,
        "digest": fingerprint(snapshot),
        "counts": {"images": len(retained) + len(reclaimable) + len(reclaimed),
                   "retained": len(retained), "reclaimable": len(reclaimable),
                   "reclaimed": len(reclaimed)},
        "freed_bytes": total,
        "retained_bytes": retained_total,
        "retained": retained,
        "reclaimable": reclaimable,
        "reclaimed": reclaimed,
    }


def execute(store, report_digest):
    cleared = False
    with store.connect(write=True) as db:
        previous = db.execute(
            "SELECT report,result,freed,executed_at FROM reclaims WHERE digest=?",
            (report_digest,)).fetchone()
        if previous:
            return _record(previous, repeated=True)
        current = report(db)
        if current["digest"] != report_digest:
            raise Problem(
                "stale_report",
                "预审报告已过期：镜像引用、登记或计划状态在预审后发生变化，请重新预审。", 409)
        targets = [(entry["id"], entry["size"]) for entry in current["reclaimable"]]
        freed, chosen = 0, []
        for image_id, size in targets:
            changed = db.execute(
                "UPDATE images SET body=x'', reclaimed=1 WHERE id=? AND reclaimed=0",
                (image_id,)).rowcount
            if changed:
                cleared = True
                freed += size
                chosen.append(image_id)
        result = {
            "format": FORMAT, "digest": report_digest, "freed_bytes": freed,
            "reclaimed": chosen, "retained": current["counts"]["retained"],
            "reclaimable": len(targets),
        }
        executed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute("INSERT INTO reclaims(digest,report,result,freed,executed_at) VALUES(?,?,?,?,?)",
                   (report_digest, canonical(current), canonical(result), freed, executed_at))
        record = _record(db.execute(
            "SELECT report,result,freed,executed_at FROM reclaims WHERE digest=?",
            (report_digest,)).fetchone(), repeated=False)
    if cleared:
        try:
            with store.connect(snapshot=False) as db:
                db.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
    return record


def _record(row, repeated):
    stored = json.loads(row["report"])
    result = json.loads(row["result"])
    chosen = set(result["reclaimed"])
    return {
        "format": "pagefold-reclaim-execution-v1",
        "digest": result["digest"],
        "executed_at": row["executed_at"],
        "freed_bytes": row["freed"],
        "reclaimed": [entry for entry in stored["reclaimable"] if entry["id"] in chosen],
        "retained_count": stored["counts"]["retained"],
        "repeated": repeated,
        "report": stored,
        "result": result,
    }


def history(db):
    return [{"digest": row["digest"], "executed_at": row["executed_at"],
             "freed_bytes": row["freed"],
             "reclaimed": len(json.loads(row["result"])["reclaimed"])}
            for row in db.execute(
                "SELECT digest,result,freed,executed_at FROM reclaims ORDER BY executed_at DESC, rowid DESC")]


def record(db, report_digest):
    row = db.execute("SELECT report,result,freed,executed_at FROM reclaims WHERE digest=?",
                     (report_digest,)).fetchone()
    if row is None:
        raise Problem("unknown_reclaim", "找不到该回收记录。", 404)
    return _record(row, repeated=False)
