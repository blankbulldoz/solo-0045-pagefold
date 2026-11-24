import json
import sqlite3
import time
import uuid

from .codec import canonical, digest
from .errors import Problem
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, plan_content
from .replay import amendments, rebuild


# A same-plan, same-revision caller waits this long for the running rehearsal before
# being told, with an explicit "running" payload, that the work is still in flight.
FOLLOWER_WAIT = 10.0
FOLLOWER_INTERVAL = 0.02
# A claim older than this is treated as abandoned (for example, the serving process
# died mid-rehearsal) and may be taken over by a later request.
ABANDONED_AFTER = 600.0


class Deferred:
    """A rehearsal that is still running after the caller's wait budget."""

    status = 202

    def __init__(self, result):
        self.result = result

    def as_dict(self):
        return self.result


def get_attempt(db, plan_id, revision):
    return db.execute("SELECT result,candidate,token,started FROM attempts "
                      "WHERE plan_id=? AND revision=?", (plan_id, revision)).fetchone()


def list_attempts(db, plan_id):
    get_plan(db, plan_id)
    return [json.loads(row[0]) for row in db.execute(
        "SELECT result FROM attempts WHERE plan_id=? ORDER BY revision DESC", (plan_id,))]


def _shared(result, how):
    return dict(result, shared=how)


def _lookup(db, plan_id, revision):
    plan = get_plan(db, plan_id)
    expect_revision(plan, revision)
    expect_open(plan)
    previous = get_attempt(db, plan_id, revision)
    return (json.loads(previous["result"]), previous["started"]) if previous is not None else (None, 0.0)


def _abandoned(result, started, now=None):
    if result is None or result.get("status") != "running":
        return False
    return (time.time() if now is None else now) - started >= ABANDONED_AFTER


def _prepare(store, plan_id, revision):
    """Read plan choices and the ancestor inside one snapshot, with no write lock."""
    with store.connect() as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        operations = amendments(items, decisions)
        ancestor = get_image(db, plan["base"], body=True)
        return operations, sum(map(len, operations.values())), ancestor["body"], \
            json.loads(ancestor["catalog"])


def _claim(store, plan_id, revision, changed):
    """Short write: record that this revision is being rehearsed, or join its owner.

    Returns ``(token, None)`` when the caller must do the work, otherwise
    ``(None, previous_result)`` for an attempt that already exists.
    """
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        previous = get_attempt(db, plan_id, revision)
        now = time.time()
        if previous is not None:
            settled = json.loads(previous["result"])
            if settled.get("status") != "running" or now - previous["started"] < ABANDONED_AFTER:
                return None, settled
        token = uuid.uuid4().hex
        running = canonical({"id": plan_id, "revision": revision, "status": "running",
                             "changed": changed, "started": now})
        if previous is None:
            db.execute("INSERT INTO attempts(plan_id,revision,result,candidate,token,started) "
                       "VALUES(?,?,?,NULL,?,?)", (plan_id, revision, running, token, now))
        else:
            db.execute("UPDATE attempts SET result=?,candidate=NULL,token=?,started=? "
                       "WHERE plan_id=? AND revision=?", (running, token, now, plan_id, revision))
        return token, None


def _follow(store, plan_id, revision, wait):
    """Wait for the current owner's result in a read-only, lock-free connection."""
    deadline = time.monotonic() + wait
    with store.connect(snapshot=False) as db:
        while True:
            row = get_attempt(db, plan_id, revision)
            if row is None:
                return {"id": plan_id, "revision": revision, "status": "running"}
            result = json.loads(row["result"])
            if result.get("status") != "running" or time.monotonic() >= deadline:
                return result
            time.sleep(min(FOLLOWER_INTERVAL, max(0.0, deadline - time.monotonic())))


def _join(store, plan_id, revision):
    current = _follow(store, plan_id, revision, FOLLOWER_WAIT)
    if current["status"] == "running":
        return Deferred(_shared(current, "rehearsal"))
    return _shared(current, "rehearsal")


def _stale(computed, code, detail):
    return {"id": computed["id"], "revision": computed["revision"], "status": "stale",
            "changed": computed["changed"],
            "reason": {"code": code, "detail": detail},
            "computed": {key: computed[key] for key in ("status", "candidate_digest", "size", "problem")
                         if key in computed}}


def _finalize(store, plan_id, revision, token, computed, candidate):
    """Short write: publish the outcome only if it still applies to the plan."""
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        current = get_attempt(db, plan_id, revision)
        if current is None:
            raise Problem("state_lost", "重演记录已被移除，请重新发起重演。", 409)
        if current["token"] != token:
            # A later claim took this revision over (for example after our claim was
            # judged abandoned).  Our computation is not evidence for that owner.
            return dict(json.loads(current["result"]), shared="concurrent")
        if plan["revision"] != revision or plan["state"] != "open":
            reason = "stale_revision" if plan["revision"] != revision else "closed_plan"
            detail = ("重演读取输入后计划版本已改变，本次结果不再适用。"
                      if reason == "stale_revision"
                      else "重演读取输入后计划已停止编辑，本次结果不再适用。")
            result, candidate = _stale(computed, reason, detail), None
        else:
            result = computed
        db.execute("UPDATE attempts SET result=?,candidate=? WHERE plan_id=? AND revision=? AND token=?",
                   (canonical(result), candidate, plan_id, revision, token))
        return result


def rehearse(store, plan_id, revision):
    with store.connect() as db:
        previous, started = _lookup(db, plan_id, revision)
    stale_claim = _abandoned(previous, started)
    if previous is not None and not stale_claim:
        return _join(store, plan_id, revision) if previous["status"] == "running" else previous

    operations, changed, ancestor, catalog = _prepare(store, plan_id, revision)
    token, previous = _claim(store, plan_id, revision, changed)
    if previous is not None:
        # Another request claimed this revision between our read and our claim.
        return _join(store, plan_id, revision) if previous["status"] == "running" \
            else _shared(previous, "rehearsal")

    candidate = None
    computed = {"id": plan_id, "revision": revision, "changed": changed}
    try:
        candidate = rebuild(ancestor, catalog, operations)
        computed.update(status="passed", candidate_digest=digest(candidate), size=len(candidate))
    except Problem as exc:
        computed.update(status="blocked", problem=exc.as_dict())
    except sqlite3.Error as exc:
        computed.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
    except Exception as exc:  # never leave a claim stuck in "running"
        computed.update(status="blocked",
                        problem={"code": "rehearsal_failed", "detail": f"{type(exc).__name__}: {exc}"})
    except BaseException:
        # Shutdown or cancellation: settle the claim as interrupted, then propagate.
        try:
            _finalize(store, plan_id, revision, token,
                      {"id": plan_id, "revision": revision, "status": "blocked", "changed": changed,
                       "problem": {"code": "interrupted", "detail": "重演在进程退出时中止。"}}, None)
        except Exception:
            pass
        raise
    outcome = _finalize(store, plan_id, revision, token, computed, candidate)
    if outcome.get("status") == "running":
        # Our claim was taken over as abandoned; report the live state, not a stale success.
        return _join(store, plan_id, revision)
    return outcome