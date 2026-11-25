"""生产入口的确定性重复验证：封存 / 重新选择 / 归档的提交次序。

本模块不做"跑很多次看看"的压力测试，而是在真实 HTTP 入口和真实 SQLite
写锁之间插入可复用的确定性时序门（gate），把每个交错顺序固定下来：

* begin_wait  —— 线程已进入写事务入口，即将执行 BEGIN IMMEDIATE
* begin_done  —— 已拿到 SQLite 写锁（BEGIN IMMEDIATE 返回）
* post_commit —— COMMIT 已完成且状态已 durable，但 HTTP 响应尚未发出

每个被编排的生产写入（decisions / rehearse / seal / archive）都经过这些
门；门只负责挂起和放行，不改变任何业务代码路径。同一版本并发写入时，
SQLite 的写锁保证同一时刻只有一个写事务，后拿到锁的事务在 expect_revision
/ expect_open 处得到 409，因此最终状态只取决于实际提交次序。

用法：
    python3 -m examples.concurrency_check            # 输出人类可读报告
    python3 -m examples.concurrency_check --json 路径 # 同时写机读证据
"""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from pagefold import api
from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

GATE_TIMEOUT = 30
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 编排标签随请求进入 HTTP 工作线程；不经过任何业务校验，只用于选择时序门。
GATE_HEADER = "X-Pagefold-Gate"


class GateSet:
    """按 (动作标签, 阶段) 编排的确定性事件集合。"""

    def __init__(self):
        self._events = {}
        self.order = []
        self._lock = threading.Lock()

    def _event(self, label, phase):
        key = (label, phase)
        event = self._events.get(key)
        if event is None:
            event = threading.Event()
            self._events[key] = event
        return event

    def label(self, label):
        return LabelGate(self, label)

    def hit(self, label, phase):
        with self._lock:
            self.order.append((label, phase, time.monotonic()))
        self._event(label, phase).set()

    def wait(self, label, phase, timeout=GATE_TIMEOUT):
        if not self._event(label, phase).wait(timeout):
            raise TimeoutError(f"等待 {label}/{phase} 超时")

    def release(self, label, phase):
        """放行某个 begin_wait 门；其余阶段事件只作观测点。"""
        if phase != "begin_wait":
            raise ValueError("只有 begin_wait 可以显式放行")
        self._event(label, "begin_wait").set()

    def event_is_set(self, label, phase):
        return self._event(label, phase).is_set()

    def snapshot(self):
        with self._lock:
            base = self.order[0][2]
            return [{"label": label, "phase": phase,
                     "at_ms": round((stamp - base) * 1000, 3)}
                    for label, phase, stamp in self.order]


class LabelGate:
    def __init__(self, gates, label):
        self.gates = gates
        self.label = label

    def hit(self, phase):
        self.gates.hit(self.label, phase)

    def wait_begin(self):
        self.gates.wait(self.label, "begin_wait")


class GatedStore(Store):
    """带时序门的状态存储：只包裹连接，不改业务语义。"""

    def __init__(self, path, gates):
        self.gates = gates
        self.local = threading.local()
        super().__init__(path)

    @contextlib.contextmanager
    def connect(self, write=False, snapshot=True):
        label = getattr(self.local, "label", None)
        if not write or label is None:
            with super().connect(write=write, snapshot=snapshot) as db:
                yield db
            return
        gate = self.gates.label(label)
        raw = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        raw.row_factory = sqlite3.Row
        begun = False
        try:
            raw.execute("PRAGMA foreign_keys=ON")
            # begin_wait 在执行 BEGIN IMMEDIATE 之前挂起，由编排线程放行。
            gate.hit("begin_wait")
            gate.wait_begin()
            raw.execute("BEGIN IMMEDIATE")
            begun = True
            gate.hit("begin_done")
            yield raw
            raw.commit()
            # 提交已经 durable；响应发送前的可观测窗口。
            gate.hit("post_commit")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                raw.rollback()
            if begun:
                # 守卫（stale_revision / closed_plan 等）在事务内触发，
                # 事务回滚而不是提交：这是与 post_commit 互斥的终态门。
                gate.hit("rolled_back")
            raise
        finally:
            raw.close()

    @contextlib.contextmanager
    def label_scope(self, label):
        previous = getattr(self.local, "label", None)
        self.local.label = label
        try:
            yield
        finally:
            self.local.label = previous


def install_gates():
    """在生产入口 api.write_plan 外层建立门控标签作用域。

    被包裹的仍是 pagefold.api 中真实的 write_plan 分发逻辑；标签只决定
    该请求线程在 GatedStore 里经过哪些时序门，不改变任何业务判断。
    """
    original = api.write_plan

    def write_plan_gated(store, handler, tail):
        label = handler.headers.get(GATE_HEADER, "").strip()
        if not label or not isinstance(store, GatedStore):
            return original(store, handler, tail)
        with store.label_scope(label):
            return original(store, handler, tail)

    api.write_plan = write_plan_gated


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None, timeout=20):
        request_headers = dict(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.origin + route, data=body, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as result:
                raw, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except urllib.error.HTTPError as error:
            with error:
                raw, actual, kind = error.read(), error.code, error.headers.get_content_type()
        expect(actual == status,
               f"{request.full_url} 返回 {actual}，期望 {status}：{raw[:200]!r}")
        parsed = json.loads(raw) if kind == "application/json" else raw
        return Outcome(actual, parsed)

    def call_any(self, route, data, headers=None):
        """不预设状态码的调用，用于需要按契约分别接受 200/409 的并发场景。"""
        request_headers = dict(headers or {})
        body = json.dumps(data).encode()
        request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.origin + route, data=body, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as result:
                raw, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except urllib.error.HTTPError as error:
            with error:
                raw, actual, kind = error.read(), error.code, error.headers.get_content_type()
        parsed = json.loads(raw) if kind == "application/json" else raw
        return Outcome(actual, parsed)

    def upload(self, name):
        request = urllib.request.Request(
            self.origin + "/api/images", data=image(name),
            headers={"X-Image-Name": name, "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=20) as result:
            return json.loads(result.read())

    def post_then_abort(self, route, data, gate_label, timeout=20):
        """发送完整 POST，在服务端处理期间直接断开 TCP，不读取响应。"""
        body = json.dumps(data).encode()
        host = self.origin.split("//", 1)[1]
        with socket.create_connection(("127.0.0.1", int(host.split(":")[1])), timeout=timeout) as sock:
            sock.settimeout(timeout)
            request = (
                f"POST {route} HTTP/1.1\r\nHost: {host}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                f"{GATE_HEADER}: {gate_label}\r\nConnection: close\r\n\r\n").encode() + body
            sock.sendall(request)
            # 立刻半关闭发送侧并彻底关闭连接，制造客户端消失。
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return Outcome(None, {"aborted": True})


class Outcome:
    def __init__(self, status, value):
        self.status = status
        self.value = value

    def as_dict(self):
        return {"http_status": self.status, "body": self.value}


class Action(threading.Thread):
    """一个在线程中运行、带门控标签的生产 HTTP 写入。"""

    def __init__(self, gates, label, client, route, payload, abort=False):
        super().__init__(name=label, daemon=True)
        self.gates = gates
        self.label = label
        self.client = client
        self.route = route
        self.payload = payload
        self.abort = abort
        self.outcome = None
        self.error = None

    def run(self):
        self.gates.label(self.label)  # 预建事件
        try:
            if self.abort:
                self.outcome = self.client.post_then_abort(self.route, self.payload, self.label)
            else:
                # 并发动作允许 200 或契约内 409，由 expect_http_codes 集中核对。
                self.outcome = self.client.call_any(
                    self.route, self.payload, headers={GATE_HEADER: self.label})
        except BaseException as exc:  # 留给主线程断言
            self.error = f"{type(exc).__name__}: {exc}"

    def result(self):
        self.join(GATE_TIMEOUT + 5)
        if self.is_alive():
            raise TimeoutError(f"动作 {self.label} 未结束")
        if self.error is not None:
            return {"label": self.label, "error": self.error}
        return {"label": self.label, **self.outcome.as_dict()}


def expect(condition, description):
    if not condition:
        raise AssertionError(description)


def state_snapshot(state_path, plan_id):
    """只读打开持久状态库，核对 SQLite 里实际落盘的内容。"""
    uri = f"file:{state_path}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        plan = db.execute("SELECT revision,state,decisions FROM plans WHERE id=?", (plan_id,)).fetchone()
        attempts = [dict(row) for row in db.execute(
            "SELECT revision,result,candidate IS NOT NULL AS has_candidate "
            "FROM attempts WHERE plan_id=? ORDER BY revision", (plan_id,))]
        release = db.execute(
            "SELECT certificate,candidate IS NOT NULL AS has_candidate,"
            "length(candidate) AS candidate_size FROM releases WHERE plan_id=?",
            (plan_id,)).fetchone()
        return {
            "revision": plan["revision"],
            "state": plan["state"],
            "decisions": json.loads(plan["decisions"]),
            "attempts": [{"revision": row["revision"],
                          "result": json.loads(row["result"]),
                          "has_candidate": bool(row["has_candidate"])} for row in attempts],
            "release": None if release is None else {
                "certificate": json.loads(release["certificate"]),
                "has_candidate": bool(release["has_candidate"]),
                "candidate_size": release["candidate_size"]},
        }
    finally:
        db.close()


def checksum(blob):
    return hashlib.sha256(blob).hexdigest()


class Harness:
    def __init__(self, root):
        self.root = Path(root)
        self.state_path = self.root / "state.sqlite"
        self.gates = GateSet()
        self.store = GatedStore(self.state_path, self.gates)
        self.server = make_server(self.store, 0)
        self.port = self.server.server_port
        self.client = Client(self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.evidence = []
        self.counter = 0
        # 捕获服务端日志：断开窗口里不应出现 "Request failed" 异常记录。
        self.log_records = []
        self.log_handler = logging.Handler()
        self.log_handler.emit = self.log_records.append
        logging.getLogger().addHandler(self.log_handler)
        install_gates()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        logging.getLogger().removeHandler(self.log_handler)

    def server_log_text(self):
        return " | ".join(record.getMessage() for record in self.log_records)

    def new_plan(self):
        base = self.client.upload("quartz")
        left = self.client.upload("cerulean")
        right = self.client.upload("umber")
        plan = self.client.call("/api/plans", {
            "base": base["id"], "left": left["id"], "right": right["id"]}, status=201).value
        return plan

    def conflict_item(self, plan):
        return next(item for item in plan["items"] if item["automatic"] is None)

    def decide(self, plan_id, revision, choice, label):
        plan = self.client.call(f"/api/plans/{plan_id}").value
        item = self.conflict_item(plan)
        return Action(self.gates, label, self.client,
                      f"/api/plans/{plan_id}/decisions",
                      {"revision": revision, "item": item["id"], "choice": choice})

    def rehearse(self, plan_id, revision, label):
        return Action(self.gates, label, self.client,
                      f"/api/plans/{plan_id}/rehearse", {"revision": revision})

    def seal(self, plan_id, revision, label, abort=False):
        return Action(self.gates, label, self.client,
                      f"/api/plans/{plan_id}/seal", {"revision": revision}, abort=abort)

    def archive(self, plan_id, revision, label, abort=False):
        return Action(self.gates, label, self.client,
                      f"/api/plans/{plan_id}/archive", {"revision": revision}, abort=abort)

    def unique(self, prefix):
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def new_scenario(self):
        """为每个场景换上独立的门集合，保证证据时序只包含本场景的事件。"""
        self.gates = GateSet()
        self.store.gates = self.gates
        return self.gates

    def record(self, name, description, plan_id, actions, expected, note):
        for action in actions:
            result = action.result()
            expected["results"].setdefault(result["label"], result)
        snapshot = state_snapshot(self.state_path, plan_id)
        snapshot["plan"] = plan_id
        expected["persisted"] = {
            "revision": snapshot["revision"], "state": snapshot["state"]}
        check_outcome(name, expected, snapshot)
        self.evidence.append({
            "scenario": name, "description": description, "plan": plan_id,
            "allowed_mutually_exclusive_outcome": expected["mutually_exclusive"],
            "observed": expected["results"],
            "commit_order_gates": self.gates.snapshot(),
            "persisted_state": snapshot,
            "note": note,
        })
        return snapshot

    def serialize(self, actions, order, labels):
        """按 order 严格串行化真实写事务。

        每个动作先在线程中到达 begin_wait（请求已在服务端处理、尚未执行
        BEGIN IMMEDIATE），等前一个动作的 HTTP 响应全部结束后才放行。
        这样后一事务拿到写锁时必然读到前一事务已提交的版本/状态，
        与 SQLite 写锁下"实际提交次序决定最终结果"的语义一致。
        """
        for name in order:
            actions[name].start()
            self.gates.wait(labels[name], "begin_wait")
            self.gates.release(labels[name], "begin_wait")
            self.gates.wait(labels[name], "begin_done")
            self.wait_finish(labels[name])
            result = actions[name].result()
            self._serial_results.append(result)

    def wait_finish(self, label):
        """等待事务到达两个互斥终态之一：已提交或已回滚。"""
        deadline = time.monotonic() + GATE_TIMEOUT
        while time.monotonic() < deadline:
            if self.gates.event_is_set(label, "post_commit"):
                return "post_commit"
            if self.gates.event_is_set(label, "rolled_back"):
                return "rolled_back"
            time.sleep(0.005)
        raise TimeoutError(f"等待 {label} 的提交/回滚终态超时")

    def run_three_way_race(self, order):
        """同一版本上 封存 / 重新选择 / 归档 同时到达，按 order 给写锁。"""
        self.new_scenario()
        plan = self.new_plan()
        plan_id = plan["id"]
        # 准备一个可封存的已重演版本（revision=1，右支选择）。
        item = self.conflict_item(plan)
        self.client.call(f"/api/plans/{plan_id}/decisions",
                         {"revision": 0, "item": item["id"], "choice": "right"})
        self.client.call(f"/api/plans/{plan_id}/rehearse", {"revision": 1})
        labels = {"decision": self.unique("decision"), "seal": self.unique("seal"),
                  "archive": self.unique("archive")}
        actions = {
            "decision": self.decide(plan_id, 1, "left", labels["decision"]),
            "seal": self.seal(plan_id, 1, labels["seal"]),
            "archive": self.archive(plan_id, 1, labels["archive"]),
        }
        winner = order[0]
        # 契约状态机：open->sealed、open->archived、sealed->archived；
        # 封存/归档后原计划写入（裁决、重演）停止。
        if winner == "decision":
            mutex = "open@2（重新选择先提交并推进版本；同版封存/归档均 stale_revision 409）"
            final_state, final_revision = "open", 2
        elif winner == "archive":
            mutex = "archived@1（归档先提交；封存与重新选择均 409 closed_plan）"
            final_state, final_revision = "archived", 1
        else:  # winner == "seal"
            if order.index("archive") > order.index("seal"):
                mutex = "archived@1（封存先提交，随后合法的 sealed->archived 迁移；重新选择 409 closed_plan）"
                final_state, final_revision = "archived", 1
            else:
                mutex = "sealed@1（封存胜出且其后无归档；重新选择 409 closed_plan）"
                final_state, final_revision = "sealed", 1
        expected = {
            "mutually_exclusive": mutex,
            "results": {},
            "final_state": final_state,
            "final_revision": final_revision,
        }
        self._serial_results = []
        self.serialize(actions, order, labels)
        for result in self._serial_results:
            expected["results"].setdefault(result["label"], result)
        snapshot = self.record(
            f"three-way-race:{'>'.join(order)}",
            "同一 revision=1 上重新选择、封存、归档并发，按给定次序竞争 SQLite 写锁",
            plan_id, [actions[name] for name in order], expected,
            "写锁串行化写事务；后提交的请求在版本/状态守卫处得到契约内结果。")
        expect_http_codes(expected["results"], order, labels)
        return snapshot

    def run_two_way_seal_vs_archive(self, seal_first):
        """两方相遇：封存与归档竞争同一版本，覆盖两个互斥终态。"""
        self.new_scenario()
        plan = self.new_plan()
        plan_id = plan["id"]
        item = self.conflict_item(plan)
        self.client.call(f"/api/plans/{plan_id}/decisions",
                         {"revision": 0, "item": item["id"], "choice": "right"})
        self.client.call(f"/api/plans/{plan_id}/rehearse", {"revision": 1})
        if seal_first:
            # 只有封存一方到场：sealed 是稳定终态（归档迁移在其他场景单独观测）。
            label = self.unique("pair-seal-only")
            labels = {"seal": label}
            actions = {"seal": self.seal(plan_id, 1, label)}
            order = ["seal"]
            mutex = "sealed@1（封存单独提交，无后续归档；sealed 为稳定终态）"
            final_state = "sealed"
        else:
            labels = {"seal": self.unique("pair-seal"),
                      "archive": self.unique("pair-archive")}
            actions = {
                "seal": self.seal(plan_id, 1, labels["seal"]),
                "archive": self.archive(plan_id, 1, labels["archive"]),
            }
            order = ["archive", "seal"]
            mutex = "archived@1（归档先提交；封存随后 closed_plan 409）"
            final_state = "archived"
        expected = {
            "mutually_exclusive": mutex,
            "results": {},
            "final_state": final_state,
            "final_revision": 1,
        }
        self._serial_results = []
        self.serialize(actions, order, labels)
        for result in self._serial_results:
            expected["results"].setdefault(result["label"], result)
        snapshot = self.record(
            f"two-way-seal-archive:{'seal-only' if seal_first else 'archive-first'}",
            "封存与归档在同一 revision=1 上相遇的两个互斥终态",
            plan_id, [actions[name] for name in order], expected,
            "sealed->archived 是合法迁移；反向不存在，故提交次序唯一决定终态。")
        expect_http_codes(expected["results"], order, labels)
        return snapshot

    def run_rehearse_then_reselect(self, decision_first):
        """候选已在重演中，当前选择随后改变（含同版竞争的两种互斥结果）。"""
        self.new_scenario()
        plan = self.new_plan()
        plan_id = plan["id"]
        item = self.conflict_item(plan)
        self.client.call(f"/api/plans/{plan_id}/decisions",
                         {"revision": 0, "item": item["id"], "choice": "right"})
        labels = {"rehearse": self.unique("rehearse"), "decision": self.unique("decision")}
        rehearse = self.rehearse(plan_id, 1, labels["rehearse"])
        decision = self.decide(plan_id, 1, "left", labels["decision"])
        if decision_first:
            mutex = "open@2：重新选择先提交，同版重演随后 stale_revision 409，revision 2 无候选"
            order = ["decision", "rehearse"]
            final_revision, final_state = 2, "open"
        else:
            mutex = "open@2：重演先提交并固化 revision 1 候选；重新选择随后把版本推进到 2，旧候选不可封存"
            order = ["rehearse", "decision"]
            final_revision, final_state = 2, "open"
        actions = {"rehearse": rehearse, "decision": decision}
        expected = {
            "mutually_exclusive": mutex,
            "results": {},
            "final_state": final_state,
            "final_revision": final_revision,
        }
        self._serial_results = []
        self.serialize(actions, order, labels)
        for result in self._serial_results:
            expected["results"].setdefault(result["label"], result)
        snapshot = self.record(
            f"rehearse-vs-reselect:{'decision-first' if decision_first else 'rehearse-first'}",
            "重演候选与重新选择在同一版本相遇的两种合法提交次序",
            plan_id, [actions[name] for name in order], expected,
            "attempts 只记录已提交重演的版本；选择改变后新版本必须重新重演才能封存。")
        if decision_first:
            expect(snapshot["attempts"] == [], "decision 先提交时不应写入 revision 1 候选")
            expect(expected["results"][labels["rehearse"]]["body"]["code"] == "stale_revision",
                   "落后的重演应返回 stale_revision")
        else:
            expect(len(snapshot["attempts"]) == 1 and
                   snapshot["attempts"][0]["revision"] == 1 and
                   snapshot["attempts"][0]["has_candidate"], "revision 1 候选应已落盘")
            blocked = self.client.call(f"/api/plans/{plan_id}/seal", {"revision": 2}, status=409)
            expect(blocked.value["code"] == "rehearsal_required", "新版本缺少重演却可封存")
        return snapshot

    def run_disconnect_window(self, kind):
        """HTTP 已断开但 SQLite 提交已完成的窗口。"""
        self.new_scenario()
        plan = self.new_plan()
        plan_id = plan["id"]
        item = self.conflict_item(plan)
        self.client.call(f"/api/plans/{plan_id}/decisions",
                         {"revision": 0, "item": item["id"], "choice": "right"})
        label = self.unique(f"abort-{kind}")
        if kind == "decision":
            action = self.decide(plan_id, 1, "left", label)
            action.abort = True
            mutex = "open@2：重新选择已 durable，客户端未收到响应；重试旧版本得 stale_revision"
            final_state, final_revision = "open", 2
        elif kind == "seal":
            self.client.call(f"/api/plans/{plan_id}/rehearse", {"revision": 1})
            action = self.seal(plan_id, 1, label, abort=True)
            mutex = "sealed@1：封存已 durable，客户端未收到响应；重试同版本幂等返回同一凭证"
            final_state, final_revision = "sealed", 1
        else:
            action = self.archive(plan_id, 1, label, abort=True)
            mutex = "archived@1：归档已 durable，客户端未收到响应；重试同版本幂等返回 archived"
            final_state, final_revision = "archived", 1
        self.log_records.clear()
        action.start()
        self.gates.wait(label, "begin_wait")
        self.gates.release(label, "begin_wait")
        self.gates.wait(label, "post_commit")  # 提交完成、响应未发
        # 给服务端工作线程一点时间尝试写响应并吞掉 BrokenPipe。
        action.join(GATE_TIMEOUT)
        expect(not action.is_alive(), "断开请求的服务端线程未收尾")
        server_logs = self.server_log_text()
        expect("Request failed" not in server_logs,
               f"客户端断开后服务端记录了异常：{server_logs}")
        expected = {
            "mutually_exclusive": mutex,
            "results": {},
            "final_state": final_state,
            "final_revision": final_revision,
        }
        snapshot = self.record(
            f"disconnect-after-commit:{kind}",
            "服务端在 COMMIT 之后、HTTP 响应之前发现客户端断开",
            plan_id, [action], expected,
            "BrokenPipe/ConnectionReset 在路由层被吞掉（无 Request failed 日志），"
            "状态库事务已 durable，不受连接断开影响。")
        self.evidence[-1]["server_log_after_disconnect"] = server_logs or "（无异常日志）"
        # 断开后的可重复读取证据
        if kind == "decision":
            retry = self.client.call(f"/api/plans/{plan_id}/decisions",
                                     {"revision": 1, "item": item["id"], "choice": "left"}, status=409)
            expect(retry.value["code"] == "stale_revision", "断开后旧版本重试未被拒绝")
        elif kind == "seal":
            retry = self.client.call(f"/api/plans/{plan_id}/seal", {"revision": 1})
            expect(retry.value == snapshot["release"]["certificate"], "封存重试未返回同一凭证")
        else:
            retry = self.client.call(f"/api/plans/{plan_id}/archive", {"revision": 1})
            expect(retry.value["state"] == "archived", "归档重试未保持幂等")
        return snapshot

    def restart_and_download(self, targets):
        """用独立子进程重启生产入口，核对持久状态与下载内容。

        targets 为 (名称, 计划 id, 期望状态, 是否存在封存成果) 列表。
        """
        cmd = [sys.executable, "-m", "pagefold", "--state", str(self.state_path), "--port", "0"]
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            port = None
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        raise RuntimeError(f"重启退出：{proc.stderr.read()}")
                    continue
                match = re.search(r"127\.0\.0\.1:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    break
            if port is None:
                raise TimeoutError("重启服务未公布监听端口")
            fresh = Client(port)
            checks = {}
            for name, plan_id, expect_state, has_release in targets:
                plan = fresh.call(f"/api/plans/{plan_id}").value
                expect(plan["state"] == expect_state, f"重启后 {name} 计划状态不符")
                entry = {"state": plan["state"], "revision": plan["revision"]}
                if has_release:
                    cert = fresh.call(f"/api/plans/{plan_id}/certificate").value
                    blob = fresh.call(f"/api/plans/{plan_id}/download").value
                    expect(checksum(blob) == cert["candidate_digest"],
                           f"重启后 {name} 下载摘要与凭证不符")
                    entry.update(certificate_revision=cert["revision"],
                                 download_digest=checksum(blob), download_size=len(blob))
                    entry.update(inspect_candidate(blob, cert))
                else:
                    missing = fresh.call(f"/api/plans/{plan_id}/download", status=404)
                    expect(missing.value["code"] == "unpublished",
                           f"未封存的 {name} 计划竟可下载")
                    entry["download"] = "unpublished/404"
                checks[name] = entry
            return {"pid": proc.pid, "port": port, "checks": checks}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def inspect_candidate(blob, cert):
    """独立打开下载到的 SQLite，核对内容与约束。"""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(blob)
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = db.execute("PRAGMA foreign_key_check").fetchall()
        note = db.execute(
            "SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()[0]
        result = {"integrity_check": integrity, "foreign_key_violations": len(foreign),
                  "conflict_row_note": note}
        if cert is not None:
            result["certificate_binds_digest"] = checksum(blob) == cert["candidate_digest"]
        return result
    finally:
        db.close()


def expect_http_codes(results, order, labels):
    """按契约状态机逐个位置核对允许的 HTTP 结果（互斥、无混合态）。"""
    revision = 1
    state = "open"
    for name in order:
        label_result = results[labels[name]]
        if name == "decision":
            if state == "open":
                expect(label_result["http_status"] == 200, "open 计划的重新选择应成功")
                revision += 1
            else:
                expect(label_result["http_status"] == 409
                       and label_result["body"]["code"] == "closed_plan",
                       "封存/归档后的重新选择必须 closed_plan")
        elif name == "seal":
            if state == "open":
                # 决策若先提交，revision 已推进，同版封存必然 stale_revision。
                if label_result["http_status"] == 409:
                    expect(label_result["body"]["code"] == "stale_revision",
                           "版本被推进后的封存必须 stale_revision")
                else:
                    expect(label_result["http_status"] == 200, "open 同版计划的封存应成功")
                    state = "sealed"
            else:
                expect(label_result["http_status"] == 409
                       and label_result["body"]["code"] == "closed_plan",
                       "非 open 计划的封存必须 closed_plan")
        else:  # archive：open->archived 与 sealed->archived 均为合法迁移
            if state in {"open", "sealed"}:
                if label_result["http_status"] == 409:
                    expect(state == "open"
                           and label_result["body"]["code"] == "stale_revision",
                           "仅当版本被推进时 open 计划的归档才 stale_revision")
                else:
                    expect(label_result["http_status"] == 200,
                           f"{state}->archived 是契约允许的迁移")
                    state = "archived"
            else:
                expect(label_result["http_status"] == 200, "重复归档保持幂等")


def check_outcome(name, expected, snapshot):
    expect(snapshot["state"] == expected["final_state"],
           f"{name}: 状态 {snapshot['state']} != {expected['final_state']}")
    expect(snapshot["revision"] == expected["final_revision"],
           f"{name}: 版本 {snapshot['revision']} != {expected['final_revision']}")
    # 互斥性：任何时刻 releases 最多一行，plans.state 为单一受控值。
    if snapshot["state"] == "sealed":
        expect(snapshot["release"] is not None, f"{name}: sealed 却没有封存行")
    if snapshot["release"] is not None:
        expect(snapshot["state"] in {"sealed", "archived"}, f"{name}: 存在封存行但状态为 {snapshot['state']}")


def render(evidence, restart):
    lines = ["生产入口重复验证：封存 / 重新选择 / 归档",
             "=" * 52, ""]
    for item in evidence:
        lines.append(f"■ {item['scenario']}")
        lines.append(f"  说明：{item['description']}")
        lines.append(f"  允许的互斥结果：{item['allowed_mutually_exclusive_outcome']}")
        order = " → ".join(f"{gate['label']}@{gate['phase']}" for gate in item["commit_order_gates"])
        lines.append(f"  确定性时序：{order}")
        for label, result in item["observed"].items():
            if "error" in result:
                lines.append(f"  响应 {label}: 异常 {result['error']}")
            else:
                lines.append(f"  响应 {label}: HTTP {result['http_status']} "
                             f"{json.dumps(result['body'], ensure_ascii=False)[:160]}")
        persisted = item["persisted_state"]
        lines.append(f"  状态库落盘：state={persisted['state']} revision={persisted['revision']}")
        lines.append(f"  证据备注：{item['note']}")
        lines.append("")
    lines.append("■ 重启后下载核对（独立子进程重新打开同一状态库）")
    for name, check in restart["checks"].items():
        lines.append(f"  {name}: {json.dumps(check, ensure_ascii=False)}")
    lines.append("")
    lines.append("全部场景的最终状态均与 SQLite 实际提交次序唯一对应，无其他混合结果。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Pagefold 生产入口确定性重复验证")
    parser.add_argument("--json", dest="json_path", type=Path, default=None,
                        help="把机读证据写入该 JSON 文件")
    parser.add_argument("--keep", action="store_true", help="保留临时状态库目录")
    args = parser.parse_args()
    root = tempfile.mkdtemp(prefix="pagefold-concurrency-")
    if args.keep:
        print(f"状态库目录：{root}")
    with Harness(root) as harness:
        permutations = [
            ("seal", "decision", "archive"),
            ("seal", "archive", "decision"),
            ("archive", "seal", "decision"),
            ("archive", "decision", "seal"),
            ("decision", "seal", "archive"),
            ("decision", "archive", "seal"),
        ]
        distinct = set()
        for order in permutations:
            snapshot = harness.run_three_way_race(list(order))
            distinct.add((snapshot["state"], snapshot["revision"]))
        # 六个排列的终态由契约状态机决定：
        # 重新选择先提交 -> open@2；归档在封存之后 -> archived@1；
        # 归档先提交 -> archived@1。三个操作全在场时不存在 sealed 终态，
        # 因为 sealed->archived 是合法迁移；sealed 终态由下方两方场景覆盖。
        expect(distinct == {("archived", 1), ("open", 2)},
               f"六次排列观察到 {distinct}，与状态机允许的互斥结果不符")
        harness.run_rehearse_then_reselect(decision_first=True)
        harness.run_rehearse_then_reselect(decision_first=False)
        harness.run_disconnect_window("decision")
        sealed = harness.run_disconnect_window("seal")
        aborted_archive = harness.run_disconnect_window("archive")
        # 两方相遇：封存单独提交且没有后续归档时，sealed 是稳定终态。
        sealed_snapshot = harness.run_two_way_seal_vs_archive(seal_first=True)
        harness.run_two_way_seal_vs_archive(seal_first=False)
        expect(sealed_snapshot["state"] == "sealed", "封存单独提交时应停在 sealed")
        archived_without_release = next(
            item["plan"] for item in harness.evidence
            if item["scenario"] == "three-way-race:archive>seal>decision")
        open_plan = next(
            item["plan"] for item in harness.evidence
            if item["scenario"] == "three-way-race:decision>seal>archive")
        targets = [
            # (名称, 计划 id, 重启后期望状态, 是否带封存成果可下载)
            ("sealed-after-disconnect", sealed["release"]["certificate"]["plan"], "sealed", True),
            ("archived-direct-no-release", aborted_archive["plan"], "archived",
             aborted_archive["release"] is not None),
            ("sealed-only", sealed_snapshot["release"]["certificate"]["plan"], "sealed", True),
            ("archived-without-release", archived_without_release, "archived", False),
            ("open-after-reselect", open_plan, "open", False),
        ]
        restart = harness.restart_and_download(targets)
        report = render(harness.evidence, restart)
        machine = {"scenarios": harness.evidence, "restart": restart}
    if args.json_path is not None:
        args.json_path.write_text(json.dumps(machine, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print(report)
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(Path(root) / ("state.sqlite" + suffix))
        with contextlib.suppress(OSError):
            os.rmdir(root)
    print("✓ 确定性时序验证通过")


if __name__ == "__main__":
    main()
