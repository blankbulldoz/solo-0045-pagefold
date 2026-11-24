"""Repeatable in-process checks for rehearsal claim sharing and invalidation.

Unlike the HTTP demonstration, these call the service functions directly so the
ordering of a slow rebuild against decisions, archiving and a concurrent seal can be
scripted exactly.  Run with ``python3 -m examples.check_rehearsal_timing``.
"""

import json
import tempfile
import threading
import time
from pathlib import Path

from pagefold import attempts
from pagefold.archive import archive
from pagefold.attempts import Deferred, list_attempts, rehearse
from pagefold.decisions import resolve
from pagefold.demo import image
from pagefold.errors import Problem
from pagefold.images import register
from pagefold.plans import create_plan
from pagefold.releases import seal
from pagefold.store import Store


def expect(condition, description):
    if not condition:
        raise AssertionError(description)


class Gate:
    """Hold the Nth rebuild call until ``release`` is set."""

    def __init__(self, blocked):
        self.blocked = set(blocked)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.original = attempts.rebuild
        self._lock = threading.Lock()

    def __enter__(self):
        attempts.rebuild = self
        return self

    def __exit__(self, *exc):
        attempts.rebuild = self.original

    def __call__(self, ancestor, catalog, operations):
        with self._lock:
            self.calls.append(sum(map(len, operations.values())))
            number = len(self.calls)
        if number in self.blocked:
            self.entered.set()
            expect(self.release.wait(15), "gate release timed out")
        return self.original(ancestor, catalog, operations)


class Runner:
    def __init__(self, work):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self.thread.start()

    def _run(self, work):
        try:
            self.result = work()
        except BaseException as exc:
            self.error = exc

    def join(self, timeout=20):
        self.thread.join(timeout)
        expect(not self.thread.is_alive(), "background work did not finish")
        if self.error:
            raise self.error
        return self.result


def setup(store):
    ids = {role: register(store, image(name), name)["id"]
           for role, name in zip(("base", "left", "right"), ("quartz", "cerulean", "umber"))}
    plan = create_plan(store, ids)
    conflict = next(item for item in plan["items"] if item["automatic"] is None)
    return plan["id"], conflict["id"], ids


def attempt_row(store, plan_id):
    with store.connect() as db:
        row = db.execute("SELECT result,candidate,token FROM attempts WHERE plan_id=? AND revision=1",
                         (plan_id,)).fetchone()
        return json.loads(row["result"]), row["candidate"], row["token"]


def check_shared_rebuild(store):
    plan_id, item, _ = setup(store)
    resolve(store, plan_id, 0, item, "right")
    with Gate(blocked={1}) as gate:
        owner = Runner(lambda: rehearse(store, plan_id, 1))
        expect(gate.entered.wait(5), "owner never reached rebuild")
        follower = Runner(lambda: rehearse(store, plan_id, 1))
        follower_done = threading.Event()
        threading.Thread(target=lambda: (follower.thread.join(), follower_done.set()), daemon=True).start()
        expect(not follower_done.wait(0.2), "follower did not wait for the running rebuild")
        running, _, _ = attempt_row(store, plan_id)
        expect(running["status"] == "running", "attempt is not readable as running")
        gate.release.set()
        first, second = owner.join(), follower.join()
    expect(first["status"] == "passed" and second["status"] == "passed", "share did not pass")
    expect("shared" not in first, "owner reported shared")
    expect(second.get("shared") == "rehearsal", "follower did not report sharing")
    expect(first["candidate_digest"] == second["candidate_digest"], "candidate differs")
    expect(gate.calls == [3], f"rebuild ran {len(gate.calls)} times for one revision")
    history = list_attempts_store(store, plan_id)
    expect(len(history) == 1, "duplicate attempt rows for one revision")
    certificate = seal(store, plan_id, 1)
    expect(certificate["candidate_digest"] == first["candidate_digest"], "seal differs from shared result")
    print("  ✓ 同版重入只 rebuild 一次，发起/重复/历史/封存一致")


def check_follower_timeout(store):
    """A follower that runs out of patience gets a readable running payload, not a hang."""
    previous_budget = attempts.FOLLOWER_WAIT
    attempts.FOLLOWER_WAIT = 0.15
    try:
        plan_id, item, _ = setup(store)
        resolve(store, plan_id, 0, item, "right")
        with Gate(blocked={1}) as gate:
            owner = Runner(lambda: rehearse(store, plan_id, 1))
            expect(gate.entered.wait(5), "owner never reached rebuild")
            deferred = rehearse(store, plan_id, 1)
            expect(isinstance(deferred, Deferred), "follower did not defer")
            expect(deferred.as_dict()["status"] == "running", "deferred payload is not running")
            gate.release.set()
            owner.join()
        final = rehearse(store, plan_id, 1)
        expect(final["status"] == "passed" and final["candidate_digest"] == owner.result["candidate_digest"],
               "settled result not reused")
        expect("shared" not in final, "reused stored attempt should be returned verbatim")
    finally:
        attempts.FOLLOWER_WAIT = previous_budget
    print("  ✓ 等待预算耗尽的重复请求得到可读 running 状态，稍后可复用已落库结果")


def check_stale_after_decision(store):
    plan_id, item, _ = setup(store)
    resolve(store, plan_id, 0, item, "right")
    with Gate(blocked={1}) as gate:
        owner = Runner(lambda: rehearse(store, plan_id, 1))
        expect(gate.entered.wait(5), "owner never reached rebuild")
        resolve(store, plan_id, 1, item, "base")  # revision becomes 2
        gate.release.set()
        stale = owner.join()
    expect(stale["status"] == "stale", f"expected stale, got {stale}")
    expect(stale["reason"]["code"] == "stale_revision", "wrong stale reason")
    expect("candidate_digest" not in stale, "stale carries a digest")
    state, candidate, _ = attempt_row(store, plan_id)
    expect(state["status"] == "stale" and candidate is None, "stale row kept its candidate")
    try:
        seal(store, plan_id, 1)
        raise AssertionError("stale attempt was sealed")
    except Problem as exc:
        expect(exc.code == "stale_revision", f"unexpected seal error {exc.code}")
    with store.connect() as db:
        expect(db.execute("SELECT count(*) FROM releases WHERE plan_id=?", (plan_id,)).fetchone()[0] == 0,
               "stale attempt produced a release")
    history = list_attempts_store(store, plan_id)
    expect([entry["status"] for entry in history] == ["stale"], "stale attempt missing from history")
    fresh = rehearse(store, plan_id, 2)
    expect(fresh["status"] == "passed", "fresh rehearsal failed")
    certificate = seal(store, plan_id, 2)
    expect(certificate["revision"] == 2, "sealed wrong revision")
    history = list_attempts_store(store, plan_id)
    expect([(e["revision"], e["status"]) for e in history] == [(2, "passed"), (1, "stale")],
           "history lost the stale record")
    print("  ✓ 写后失效：stale 可读、无候选、不可封存，病史保留旧失效记录")


def check_stale_after_archive(store):
    plan_id, item, _ = setup(store)
    resolve(store, plan_id, 0, item, "right")
    with Gate(blocked={1}) as gate:
        owner = Runner(lambda: rehearse(store, plan_id, 1))
        expect(gate.entered.wait(5), "owner never reached rebuild")
        archive(store, plan_id, 1)
        gate.release.set()
        stale = owner.join()
    expect(stale["status"] == "stale" and stale["reason"]["code"] == "closed_plan", "archive stale wrong")
    try:
        seal(store, plan_id, 1)
        raise AssertionError("archived plan was sealed")
    except Problem as exc:
        expect(exc.code == "closed_plan", f"unexpected seal error {exc.code}")
    print("  ✓ 读后归档：旧结果标为 closed_plan 失效，归档计划不可封存")


def check_seal_while_running(store):
    """A concurrent seal must see running and refuse instead of sealing a half-written attempt."""
    plan_id, item, _ = setup(store)
    resolve(store, plan_id, 0, item, "right")
    with Gate(blocked={1}) as gate:
        owner = Runner(lambda: rehearse(store, plan_id, 1))
        expect(gate.entered.wait(5), "owner never reached rebuild")
        try:
            seal(store, plan_id, 1)
            raise AssertionError("seal succeeded while rehearsal was running")
        except Problem as exc:
            expect(exc.code == "rehearsal_running", f"unexpected seal error {exc.code}")
        gate.release.set()
        owner.join()
    expect(seal(store, plan_id, 1)["revision"] == 1, "seal after settling failed")
    print("  ✓ 重演进行中封存得到 rehearsal_running，完成后可封存")


def check_plans_do_not_freeze(store):
    """Two plans write decisions and seal while a third plan's rebuild is in flight."""
    big, big_item, _ = setup(store)
    other, other_item, _ = setup(store)
    resolve(store, big, 0, big_item, "right")
    with Gate(blocked={1}) as gate:
        owner = Runner(lambda: rehearse(store, big, 1))
        expect(gate.entered.wait(5), "owner never reached rebuild")
        started = time.perf_counter()
        resolve(store, other, 0, other_item, "right")
        rehearse(store, other, 1)
        seal(store, other, 1)
        elapsed = time.perf_counter() - started
        expect(elapsed < 3.0, f"other plan waited {elapsed:.1f}s behind the rebuild")
        gate.release.set()
        owner.join()
    print(f"  ✓ 另一计划在重演期间完成裁决/重演/封存，用时 {elapsed * 1000:.0f} ms")


def check_abandoned_claim(store):
    """A claim whose owner died is taken over once it exceeds the abandonment window."""
    plan_id, item, _ = setup(store)
    resolve(store, plan_id, 0, item, "right")
    with store.connect(write=True) as db:
        db.execute("INSERT INTO attempts(plan_id,revision,result,candidate,token,started) "
                   "VALUES(?,?,?,NULL,'dead',?)",
                   (plan_id, 1, json.dumps({"id": plan_id, "revision": 1, "status": "running",
                                            "changed": 3, "started": 0}), 0))
    with Gate(blocked={1}) as gate:
        owner = Runner(lambda: rehearse(store, plan_id, 1))
        expect(gate.entered.wait(5), "abandoned claim was not taken over")
        gate.release.set()
        result = owner.join()
    expect(result["status"] == "passed", "takeover did not pass")
    print("  ✓ 过期未完成的占位可被后续请求接管")


def check_claim_race(store):
    """Both requests read no attempt; the first claim wins and the second joins it."""
    plan_id, item, _ = setup(store)
    resolve(store, plan_id, 0, item, "right")
    local = threading.local()
    original = attempts._lookup

    def dispatch(db, plan_id_, revision):
        if getattr(local, "stale_read", False):
            # Simulate a caller whose fast-path read happened before the winner claimed.
            return None, 0.0
        return original(db, plan_id_, revision)

    def racing_loser():
        local.stale_read = True
        return rehearse(store, plan_id, 1)

    attempts._lookup = dispatch
    try:
        with Gate(blocked={1}) as gate:
            winner = Runner(lambda: rehearse(store, plan_id, 1))
            expect(gate.entered.wait(5), "winner never reached rebuild")
            loser = Runner(racing_loser)
            time.sleep(0.2)
            expect(loser.thread.is_alive(), "racing loser did not join the winner's claim")
            gate.release.set()
            first, second = winner.join(), loser.join()
    finally:
        attempts._lookup = original
    expect(first["status"] == "passed" and second["status"] == "passed", "claim race did not share")
    expect(second.get("shared") == "rehearsal", "claim loser did not report sharing")
    expect(first["candidate_digest"] == second["candidate_digest"], "claim race produced two candidates")
    expect(len(gate.calls) == 1, f"claim race rebuilt {len(gate.calls)} times")
    history = list_attempts_store(store, plan_id)
    expect(len(history) == 1 and history[0]["status"] == "passed", "claim race left a bad history")
    print("  ✓ 读取与占位之间被抢先时，后到请求加入领先者而不是另起重演")


def list_attempts_store(store, plan_id):
    with store.connect() as db:
        return list_attempts(db, plan_id)


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-checks-") as root:
        store = Store(Path(root) / "state.sqlite")
        print("rehearsal timing checks")
        check_shared_rebuild(store)
        check_follower_timeout(store)
        check_stale_after_decision(store)
        check_stale_after_archive(store)
        check_seal_while_running(store)
        check_plans_do_not_freeze(store)
        check_abandoned_claim(store)
        check_claim_race(store)
    print("checks ✓")


if __name__ == "__main__":
    main()
