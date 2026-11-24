"""Controlled-timing evidence for same-revision rehearsal sharing and invalidation.

Run with ``python3 -m examples.rehearsal_timing``.  The script starts a real HTTP
server, uploads synthetic images and drives the public endpoints.  It replaces the
module-level ``rebuild`` step with a gate so the asynchronous order of a rehearsal
and the writes that race it is deterministic instead of timing-dependent.
"""

import argparse
import hashlib
import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold import attempts
from pagefold.demo import image
from pagefold.server import Handler, make_server
from pagefold.store import Store

Handler.log_message = lambda *args, **kwargs: None


def expect(condition, description):
    if not condition:
        raise AssertionError(description)


def say(message):
    print("  " + message, flush=True)


class Client:
    """Minimal JSON client that reports how long each call took."""

    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None, timeout=20):
        request_headers = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=request_headers)
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        elapsed = time.perf_counter() - started
        expect(actual == status, f"{route}: HTTP {actual} != {status}: {body[:300]}")
        parsed = json.loads(body) if kind == "application/json" else body
        return {"status": actual, "body": parsed, "seconds": elapsed}

    def upload(self, name, payload=None):
        return self.call("/api/images", image(name) if payload is None else payload, 201, {
            "X-Image-Name": name, "Content-Type": "application/octet-stream"})["body"]


class Gate:
    """Block selected rebuild calls on one event until the demo releases them."""

    def __init__(self, blocked):
        self.blocked = set(blocked)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.original = attempts.rebuild
        self._lock = threading.Lock()

    def install(self):
        attempts.rebuild = self

    def restore(self):
        attempts.rebuild = self.original

    def __call__(self, ancestor, catalog, operations):
        with self._lock:
            self.calls += 1
            number = self.calls
        if number in self.blocked:
            self.entered.set()
            expect(self.release.wait(15), "演示等待释放超时")
        return self.original(ancestor, catalog, operations)

class Running:
    """Run one request in a background thread and keep its outcome."""

    def __init__(self, client, route, data):
        self.started = time.perf_counter()
        self.outcome = None
        self.error = None
        self.thread = threading.Thread(target=self._run, args=(client, route, data), daemon=True)
        self.thread.start()

    def _run(self, client, route, data):
        try:
            self.outcome = client.call(route, data)
        except BaseException as exc:  # surfaced by join()
            self.error = exc

    def join(self, timeout=20):
        self.thread.join(timeout)
        expect(not self.thread.is_alive(), "后台请求没有结束")
        if self.error:
            raise self.error
        return self.outcome

    @property
    def running(self):
        return self.thread.is_alive()


def wait_for(gate, label):
    expect(gate.entered.wait(5), f"重演没有进入受控的 {label} 阶段")
    return time.perf_counter()


def conflict(plan):
    return next(item for item in plan["items"] if item["automatic"] is None)


def body(result):
    return result["body"]


def attempts_of(client, plan_id):
    return body(client.call(f"/api/plans/{plan_id}/attempts"))


def make_plan(client, sources):
    return body(client.call("/api/plans", sources, 201))


def decide(client, plan_id, item, choice, revision):
    return client.call(f"/api/plans/{plan_id}/decisions",
                       {"item": item, "choice": choice, "revision": revision})


def rehearse(client, plan_id, revision, expected=200):
    return client.call(f"/api/plans/{plan_id}/rehearse", {"revision": revision}, expected)


def phase_sharing(client, sources, timeline):
    print("\n[1] 同版重入：同一计划同一版只重演一次，重复请求明确共享结果", flush=True)
    plan_a = make_plan(client, sources)
    item_a = conflict(plan_a)["id"]
    decide(client, plan_a["id"], item_a, "right", 0)
    plan_b = make_plan(client, sources)
    item_b = conflict(plan_b)["id"]

    gate = Gate(blocked={1})
    gate.install()
    try:
        owner = Running(client, f"/api/plans/{plan_a['id']}/rehearse", {"revision": 1})
        entered_at = wait_for(gate, "rebuild")
        timeline.append({"event": "plan A 重演进入 rebuild", "at": entered_at, "gate_calls": gate.calls})
        say("计划 A 的重演已占用 rebuild 阶段，状态库写事务已经结束")

        while_running = client.call(f"/api/plans/{plan_a['id']}/attempts")
        record = body(while_running)[0]
        expect(record["status"] == "running", "处理中的重演没有可读状态")
        expect(record["id"] == plan_a["id"] and record["revision"] == 1, "处理中记录归属错误")
        say(f"处理中可读：GET attempts -> {record['status']}（{while_running['seconds'] * 1000:.0f} ms）")
        page = client.call(f"/plans/{plan_a['id']}")
        expect("处理中".encode() in body(page), "计划页没有显示处理中状态")
        say("计划页同步显示“处理中”")

        decision = decide(client, plan_b["id"], item_b, "right", 0)
        expect(body(decision)["revision"] == 1, "另一计划的裁决没有推进版本")
        expect(decision["seconds"] < 2.0, "另一计划保存裁决被重演阻塞")
        timeline.append({"event": "计划 B 在 A 重演期间保存裁决", "seconds": decision["seconds"]})
        say(f"另一计划同时保存裁决：HTTP 200，第 {body(decision)['revision']} 版，{decision['seconds'] * 1000:.0f} ms")

        replay_b = rehearse(client, plan_b["id"], 1)
        expect(body(replay_b)["status"] == "passed", "另一计划重演未通过")
        seal_b = client.call(f"/api/plans/{plan_b['id']}/seal", {"revision": 1})
        expect(body(seal_b)["revision"] == 1, "另一计划封存版本错误")
        say(f"另一计划在 A 阻塞期间完成重演并封存（{replay_b['seconds'] * 1000:.0f} ms / "
            f"{seal_b['seconds'] * 1000:.0f} ms），两个计划未相互冻结")

        follower = Running(client, f"/api/plans/{plan_a['id']}/rehearse", {"revision": 1})
        time.sleep(0.25)
        expect(follower.running, "同版重复请求没有等待正在进行的重演")
        gate.release.set()
        first, second = owner.join(), follower.join()
        expect(first["body"]["status"] == "passed", "发起重演未通过")
        expect("shared" not in first["body"], "发起重演被标记为共享结果")
        expect(second["body"].get("shared") == "rehearsal", "同版重复请求没有标明共享本次重演")
        expect(second["body"]["candidate_digest"] == first["body"]["candidate_digest"],
               "共享结果与发起结果候选不一致")
        expect(gate.calls == 2, f"同一版被重复重演：rebuild 调用 {gate.calls} 次（A 与 B 各一次）")
        timeline.append({"event": "同版重入返回共享结果", "seconds": second["seconds"],
                         "shared": second["body"]["shared"], "rebuild_calls": gate.calls})
        say(f"同版重入：发起请求通过，重复请求 shared=rehearsal，rebuild 共调用 {gate.calls} 次（A、B 各一次）")
        say(f"重复请求等待 {second['seconds'] * 1000:.0f} ms 后取得同一次重演的候选摘要")
    finally:
        gate.restore()

    history = attempts_of(client, plan_a["id"])
    expect([entry["revision"] for entry in history] == [1], "同一版产生了多条重演记录")
    expect(history[0]["status"] == "passed", "共享后历史记录不是通过")
    seal_a = client.call(f"/api/plans/{plan_a['id']}/seal", {"revision": 1})
    certificate = body(seal_a)
    expect(certificate["revision"] == 1 and certificate["candidate_digest"] == history[0]["candidate_digest"],
           "共享重演结果不能用于封存")
    say("共享的重演记录可用于当前版封存，历史只有一条")
    return {"id": plan_a["id"], "certificate": certificate,
            "history": [(entry["revision"], entry["status"]) for entry in history]}


def phase_invalidation(client, sources, timeline):
    print("\n[2] 写后失效：重演读完输入后裁决改变，旧结果标为失效且不可封存", flush=True)
    plan = make_plan(client, sources)
    item = conflict(plan)["id"]
    decide(client, plan["id"], item, "right", 0)
    first = rehearse(client, plan["id"], 1)
    expect(body(first)["status"] == "passed", "基线重演未通过")
    changed = decide(client, plan["id"], item, "base", 1)
    expect(body(changed)["revision"] == 2, "第二次裁决没有推进版本")

    gate = Gate(blocked={1})
    gate.install()
    try:
        owner = Running(client, f"/api/plans/{plan['id']}/rehearse", {"revision": 2})
        entered_at = wait_for(gate, "rebuild")
        timeline.append({"event": "计划 C 第 2 版重演进入 rebuild", "at": entered_at})
        say("第 2 版重演已读完输入并进入 rebuild，此时裁决被改写")
        raced = decide(client, plan["id"], item, "right", 2)
        expect(body(raced)["revision"] == 3, "并发裁决没有推进到第 3 版")
        expect(raced["seconds"] < 2.0, "重演阻塞了同一计划的裁决写入")
        say(f"重演阻塞期间保存裁决：第 2 版 -> 第 3 版，{raced['seconds'] * 1000:.0f} ms")
        gate.release.set()
        stale = owner.join()
        record = body(stale)
        expect(record["status"] == "stale", "写后结果没有标为失效")
        expect(record["reason"]["code"] == "stale_revision", "失效原因不是版本改变")
        expect("candidate_digest" not in record and "candidate" not in record, "失效结果仍携带候选")
        expect("shared" not in record, "失效结果被标记为共享成功")
        timeline.append({"event": "旧结果落库为 stale", "status": record["status"],
                         "reason": record["reason"]["code"]})
        say(f"旧结果返回 status=stale，reason={record['reason']['code']}，无可下载候选")
    finally:
        gate.restore()

    history = attempts_of(client, plan["id"])
    expect([entry["revision"] for entry in history] == [2, 1], "失效记录没有进入历史")
    expect(history[0]["status"] == "stale" and history[0]["reason"]["code"] == "stale_revision",
           "历史中的失效记录状态不可读")
    expect(history[1]["status"] == "passed", "旧版通过记录丢失")
    page = client.call(f"/plans/{plan['id']}")
    expect("已失效".encode() in body(page), "计划页没有显示已失效状态")
    say("历史同时保留第 2 版“已失效”和第 1 版“通过”，计划页显示“已失效”")

    blocked = client.call(f"/api/plans/{plan['id']}/seal", {"revision": 3}, 409)
    expect(body(blocked)["code"] == "rehearsal_required", "失效版本被允许封存")
    expect(client.call(f"/api/plans/{plan['id']}/download", status=404)["body"]["code"] == "unpublished",
           "失效版本产生了可下载候选")
    old = client.call(f"/api/plans/{plan['id']}/seal", {"revision": 2}, 409)
    expect(body(old)["code"] == "stale_revision", "旧版封存没有被拒绝")
    say("发布资格：第 3 版无当前重演证据，封存与下载均被拒绝；第 2 版封存因过期被拒绝")

    current = rehearse(client, plan["id"], 3)
    expect(body(current)["status"] == "passed", "重新重演未通过")
    certificate = body(client.call(f"/api/plans/{plan['id']}/seal", {"revision": 3}))
    candidate = client.call(f"/api/plans/{plan['id']}/download")
    expect(certificate["revision"] == 3, "封存版本不是第 3 版")
    expect(hashlib.sha256(candidate["body"]).hexdigest() == certificate["candidate_digest"],
           "下载候选与凭证摘要不一致")
    final = attempts_of(client, plan["id"])
    expect([(entry["revision"], entry["status"]) for entry in final] ==
           [(3, "passed"), (2, "stale"), (1, "passed")], "最终历史没有保留失效与旧通过记录")
    expect(history[0]["revision"] == 2 and history[0]["status"] == "stale", "失效记录不在历史中")
    say("重新重演第 3 版通过后封存：凭证摘要与下载字节一致；"
        "历史为 3 通过 / 2 失效 / 1 通过")
    timeline.append({"event": "第 3 版重演通过并封存", "certificate_revision": certificate["revision"],
                     "history": [(entry["revision"], entry["status"]) for entry in final]})
    return {"id": plan["id"], "certificate": certificate,
            "history": [(entry["revision"], entry["status"]) for entry in final]}


def phase_archive(client, sources, timeline):
    print("\n[3] 补充：重演读完输入后计划归档，旧结果同样标为失效", flush=True)
    plan = make_plan(client, sources)
    item = conflict(plan)["id"]
    decide(client, plan["id"], item, "right", 0)

    gate = Gate(blocked={1})
    gate.install()
    try:
        owner = Running(client, f"/api/plans/{plan['id']}/rehearse", {"revision": 1})
        wait_for(gate, "rebuild")
        archived = client.call(f"/api/plans/{plan['id']}/archive", {"revision": 1})
        expect(body(archived)["state"] == "archived", "归档未生效")
        say("重演阻塞期间归档计划：HTTP 200，状态 archived")
        gate.release.set()
        stale = owner.join()
        expect(body(stale)["status"] == "stale", "归档后旧结果没有标为失效")
        expect(body(stale)["reason"]["code"] == "closed_plan", "归档失效原因不正确")
        say(f"旧结果返回 status=stale，reason={body(stale)['reason']['code']}")
    finally:
        gate.restore()

    history = attempts_of(client, plan["id"])
    expect(len(history) == 1 and history[0]["status"] == "stale", "归档失效记录没有保留")
    denied = client.call(f"/api/plans/{plan['id']}/seal", {"revision": 1}, 409)
    expect(body(denied)["code"] == "closed_plan", "归档计划仍可封存")
    expect(client.call(f"/api/plans/{plan['id']}/download", status=404)["body"]["code"] == "unpublished",
           "归档失效产生了候选")
    timeline.append({"event": "归档后旧结果失效", "reason": body(stale)["reason"]["code"]})
    say("历史保留“已失效”；归档计划封存与下载均被拒绝")
    return {"id": plan["id"], "history": [(entry["revision"], entry["status"]) for entry in history]}


def main():
    parser = argparse.ArgumentParser(description="验证同版重演共享与写后失效")
    parser.add_argument("--report", type=Path, help="可选：把时序证据写入 JSON 文件")
    args = parser.parse_args()
    logging.getLogger("http.server").setLevel(logging.ERROR)
    timeline = []
    with tempfile.TemporaryDirectory(prefix="pagefold-timing-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = Client(server.server_port)
                base = client.upload("quartz")
                left = client.upload("cerulean")
                right = client.upload("umber")
                sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
                timeline.append({"event": "镜像与计划就绪", "sources": sources})
                shared = phase_sharing(client, sources, timeline)
                invalidated = phase_invalidation(client, sources, timeline)
                archived = phase_archive(client, sources, timeline)
            finally:
                server.shutdown()
                thread.join(timeout=5)
    report = {
        "evidence": "pagefold rehearsal sharing and invalidation",
        "checks": ["same-version re-entry shares one rebuild",
                   "other plans write decisions/seal while a large rebuild runs",
                   "write-after-read invalidation is readable and not releaseable",
                   "archive-after-read invalidation is readable and not releaseable"],
        "plans": {"shared": shared, "invalidated": invalidated, "archived": archived},
        "timeline": timeline,
    }
    print("\n证据摘要", flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nrehearsal-timing ✓ 同版共享、跨计划并发、写后失效与归档失效均符合预期", flush=True)


if __name__ == "__main__":
    main()
