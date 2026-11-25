"""Pagefold HTTP→WSGI 迁移对照演练。

在同一进程内并排拉起两套协议：

* 旧协议：``examples/_legacy_http.py`` 保留的 http.server 处理器（入口标记 ``http``）；
* 新协议：``pagefold.server`` 承载的标准 WSGI 应用（入口标记 ``wsgi``）。

两套服务各自使用独立状态库，但接收完全相同的请求序列，随后比较：

1. 版本（``revision`` 推进、过期版本、幂等、封存/归档状态）；
2. 来源（镜像按字节 SHA-256 寻址的 ID、计划三方来源、凭证 sources）；
3. 内容摘要（HTML/JSON 归一化摘要、候选 SQLite 的 candidate_digest）；
4. 失败语义（状态码、问题 code、内容类型）。

同时覆盖三类约束场景：
上限附近的 SQLite 请求、多 worker 同时操作同一计划、代理转发后的浏览器表单回跳。

用法::

    python3 -m examples.wsgi_drill              # 运行全部演练
    python3 -m examples.wsgi_drill --report out.json
"""

import argparse
import hashlib
import http.client
import io
import json
import re
import socket
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode

from pagefold.demo import image
from pagefold.server import make_server as make_wsgi_server
from pagefold.store import Store
from pagefold.wsgi import make_app

from examples._legacy_http import make_server as make_legacy_server

STACKS = ("legacy-http", "new-wsgi")


# ---------------------------------------------------------------- 通用工具

def stable_digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    # 归一化所有运行期 ID/摘要：完整 64 位镜像摘要、32 位计划 ID 及其 10–14 位前缀。
    masked = re.compile(rb"[0-9a-f]{8,64}").sub(b"<id>", data)
    return hashlib.sha256(masked).hexdigest()


def jsonish(data):
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data
    return str(data)


def fail(condition, message):
    if not condition:
        raise AssertionError(message)


class Client:
    """不跟随跳转、可改写 Host 头的最小 HTTP 客户端。"""

    def __init__(self, port, host=None):
        self.port = port
        self.host = host or f"127.0.0.1:{port}"

    def call(self, route, data=None, status=None, headers=None):
        request_headers = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers.setdefault("Content-Type", "application/json")
        # 显式 Host：用 skip_host 阻止 http.client 自动生成，改由头里携带。
        skip_host = "Host" in request_headers or self.host != f"127.0.0.1:{self.port}"
        if skip_host:
            request_headers.setdefault("Host", self.host)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            if skip_host:
                method = "POST" if data is not None else "GET"
                connection.putrequest(method, route, skip_host=True)
                for key, value in request_headers.items():
                    connection.putheader(key, value)
                if data is None:
                    connection.endheaders()
                else:
                    connection.endheaders()
                    connection.send(data)
            else:
                connection.request("POST" if data is not None else "GET", route,
                                   body=data, headers=request_headers)
            result = connection.getresponse()
            body = result.read()
            actual, kind, location = (result.status, result.getheader("Content-Type", ""),
                                      result.getheader("Location"))
            entry = result.getheader("X-Pagefold-Entry")
        finally:
            connection.close()
        if status is not None:
            fail(actual == status, f"{route}: {actual} != {status}: {body[:200]!r}")
        parsed = None
        if kind.startswith("application/json"):
            parsed = json.loads(body)
        return {"status": actual, "kind": kind, "body": body, "json": parsed,
                "location": location, "entry": entry}

    def raw(self, method, route, raw_bytes, extra_headers=(), send_body=True):
        """绕过 http.client 自行拼装请求，用于缺失 Content-Length 等畸形报文。

        send_body=False 时彻底不发送请求体，模拟“声明了 POST 但没有
        Content-Length、也没有 body”的报文，服务器应在长度校验处直接拒绝。
        """
        request = [f"{method} {route} HTTP/1.1"]
        if not any(line.lower().startswith("host:") for line in extra_headers):
            request.append(f"Host: {self.host}")
        request.extend([*extra_headers, "Connection: close", "", ""])
        with socket.create_connection(("127.0.0.1", self.port), timeout=30) as stream:
            stream.sendall("\r\n".join(request).encode("ascii")
                           + (raw_bytes if send_body else b""))
            chunks = []
            while True:
                block = stream.recv(65536)
                if not block:
                    break
                chunks.append(block)
        text = b"".join(chunks)
        head, _, body = text.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
        return int(status_split(status_line)), body, head.decode("ascii", "replace")


def status_split(line):
    return line.split(" ", 2)[1]


def tolerant_upload(port, host, route, body, content_type="application/octet-stream",
                    extra_headers=()):
    """发送可能被服务器提前拒绝的大请求体，容忍发送期连接重置并读取拒绝响应。"""
    head = "\r\n".join([
        f"POST {route} HTTP/1.1", f"Host: {host}", f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}", *extra_headers, "Connection: close", "", ""]
    ).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=30) as stream:
        try:
            stream.sendall(head + body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        chunks = []
        while True:
            try:
                block = stream.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not block:
                break
            chunks.append(block)
    text = b"".join(chunks)
    raw_head, _, response_body = text.partition(b"\r\n\r\n")
    status_line = raw_head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    kind = ""
    for line in raw_head.decode("ascii", "replace").split("\r\n"):
        if line.lower().startswith("content-type:"):
            kind = line.split(":", 1)[1].strip()
    return {"status": int(status_split(status_line)), "body": response_body, "kind": kind,
            "json": json.loads(response_body) if response_body[:1] == b"{" else None}


def start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def near_limit_image():
    """构造逼近 4 MiB 上限的合法 SQLite 镜像（单表、显式 BINARY 主键、无溢出页）。"""
    low, high = 100, 20_000
    best = None

    def build(count):
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE bulk(id TEXT PRIMARY KEY COLLATE BINARY, payload BLOB) "
                       "WITHOUT ROWID")
            db.executemany("INSERT INTO bulk VALUES(?,?)",
                           ((f"row-{index:05d}", b"\x5a" * 200) for index in range(count)))
            return db.serialize()
        finally:
            db.close()

    while low <= high:
        middle = (low + high) // 2
        data = build(middle)
        if len(data) < 4 * 1024 * 1024:
            best = data
            low = middle + 1
        else:
            high = middle - 1
    fail(best is not None and 3_900_000 < len(best) < 4 * 1024 * 1024,
         f"逼近上限的镜像体积异常：{len(best) if best else None}")
    return best


# ---------------------------------------------------------------- 并行工作流

class Stack:
    def __init__(self, name, server):
        self.name = name
        self.server = server
        self.client = Client(server.server_port)
        self.state = {}

    def call(self, route, data=None, status=None, headers=None):
        return self.client.call(route, data, status, headers)


def workflow(stack):
    """与 walkthrough 等价的生产工作流，记录每一步的可比较结果。"""
    s = stack.state
    probes = []

    def record(label, response):
        probes.append({"label": label, "status": response["status"],
                       "kind": response["kind"].split(";", 1)[0],
                       "digest": stable_digest(jsonish(response["json"] or response["body"])),
                       "entry": response["entry"],
                       "location": response["location"]})
        return response

    record("首页 HTML", stack.call("/"))
    s["base"] = record("登记祖本", stack.call("/api/images", image("quartz"), 201, {
        "X-Image-Name": "quartz", "Content-Type": "application/octet-stream"}))["json"]
    record("重复镜像复用", stack.call("/api/images", image("quartz"), 201, {
        "X-Image-Name": "again", "Content-Type": "application/octet-stream"}))
    record("坏镜像拒绝", stack.call("/api/images", b"broken", 422, {
        "Content-Type": "application/octet-stream"}))
    s["left"] = stack.call("/api/images", image("cerulean"), 201, {
        "X-Image-Name": "cerulean", "Content-Type": "application/octet-stream"})["json"]
    s["right"] = stack.call("/api/images", image("umber"), 201, {
        "X-Image-Name": "umber", "Content-Type": "application/octet-stream"})["json"]
    catalog = record("镜像目录 JSON", stack.call(f"/api/images/{s['base']['id']}"))["json"]
    schema_signature = catalog["catalog"]["signature"]
    record("镜像目录 HTML", stack.call(f"/images/{s['base']['id']}"))
    record("行窗口 JSON", stack.call(f"/api/images/{s['base']['id']}/rows?table=node"))
    record("行窗口 HTML", stack.call(
        f"/images/{s['base']['id']}/rows?" + urlencode({"table": "node"})))
    record("未知镜像 404", stack.call("/api/images/missing", status=404))
    record("未知表 404",
           stack.call(f"/api/images/{s['base']['id']}/rows?table=none", status=404))

    sources = {"base": s["base"]["id"], "left": s["left"]["id"], "right": s["right"]["id"]}
    alternate_db = sqlite3.connect(":memory:")
    try:
        alternate_db.deserialize(image("quartz"))
        alternate_db.execute("CREATE INDEX bench_label_sample ON bench(label)")
        alternate = alternate_db.serialize()
    finally:
        alternate_db.close()
    alt = stack.call("/api/images", alternate, 201, {
        "Content-Type": "application/octet-stream"})["json"]
    record("结构不一致 409",
           stack.call("/api/plans", dict(sources, right=alt["id"]), 409))
    plan = record("创建计划 201", stack.call("/api/plans", sources, 201))["json"]
    s["plan"] = plan
    route = f"/api/plans/{plan['id']}"
    record("计划详情", stack.call(route))
    record("计划页 HTML", stack.call(f"/plans/{plan['id']}"))
    record("未决重演 409", stack.call(route + "/rehearse", {"revision": 0}, 409))

    conflict = next(item for item in plan["items"] if not item["automatic"])
    decision = {"item": conflict["id"], "choice": "right", "revision": 0}
    first = record("首次裁决推进版本", stack.call(route + "/decisions", decision, 200))["json"]
    s["revision"] = first["revision"]
    record("过期版本 409", stack.call(route + "/decisions", decision, 409))
    record("幂等裁决不推进",
           stack.call(route + "/decisions", dict(decision, revision=1), 200))
    passed = record("重演通过", stack.call(route + "/rehearse", {"revision": 1}))["json"]
    s["candidate_digest"] = passed["candidate_digest"]
    record("同版重演复用", stack.call(route + "/rehearse", {"revision": 1}))

    stress = {"base": s["base"]["id"],
              "left": stack.call("/api/images", image("stress-left"), 201, {
                  "Content-Type": "application/octet-stream"})["json"]["id"],
              "right": stack.call("/api/images", image("stress-right"), 201, {
                  "Content-Type": "application/octet-stream"})["json"]["id"]}
    failed_plan = stack.call("/api/plans", stress, 201)["json"]
    stress_route = f"/api/plans/{failed_plan['id']}"
    record("约束冲突重演受阻",
           stack.call(stress_route + "/rehearse", {"revision": 0}, 200))
    record("失败候选不可下载", stack.call(stress_route + "/download", status=404))
    record("失败候选不可颁证", stack.call(stress_route + "/certificate", status=404))

    stack.call(route + "/decisions", dict(decision, revision=1, choice="base"), 200)
    record("重演历史保留", stack.call(route + "/attempts"))
    record("旧版本不可封存", stack.call(route + "/seal", {"revision": 2}, 409))
    current = stack.call(route + "/rehearse", {"revision": 2})["json"]
    certificate = record("封存成功", stack.call(route + "/seal", {"revision": 2}, 200))["json"]
    record("重复封存稳定", stack.call(route + "/seal", {"revision": 2}, 200))
    record("封存后拒绝裁决",
           stack.call(route + "/decisions", dict(decision, revision=2), 409))
    proof = record("凭证下载", stack.call(route + "/certificate"))["json"]
    candidate = record("候选下载", stack.call(route + "/download"))
    record("归档", stack.call(route + "/archive", {"revision": 2}, 200))
    record("归档后拒绝重演", stack.call(route + "/rehearse", {"revision": 2}, 409))
    record("归档后仍可下载", stack.call(route + "/download"))
    record("归档后仍可取凭证", stack.call(route + "/certificate"))

    fail(proof["sources"] == sources, "凭证来源与三方镜像不一致")
    fail(proof["candidate_digest"] == current["candidate_digest"], "凭证候选摘要不一致")
    fail(hashlib.sha256(candidate["body"]).hexdigest() == proof["candidate_digest"],
         "下载字节与凭证摘要不一致")
    fail(catalog["catalog"]["signature"] == proof["schema"], "凭证模式摘要不一致")
    return probes, {"sources": sources, "certificate": certificate,
                    "candidate_digest": proof["candidate_digest"],
                    "candidate_size": len(candidate["body"]),
                    "schema_signature": schema_signature}


# ------------------------------------------------- 失败语义矩阵（两栈同输入）

def failure_matrix(stack):
    s = stack.state
    route = f"/api/plans/{s['plan']['id']}"
    cases = [
        ("坏 SQLite 镜像", ("POST", "/api/images", b"broken",
                           {"Content-Type": "application/octet-stream"}, 422)),
        ("错误媒体类型", ("POST", "/api/images", b"x",
                         {"Content-Type": "text/plain"}, 415)),
        # 显式声明正确媒体类型、唯独不给 Content-Length，隔离长度校验（411）。
        ("缺少内容长度", ("RAW-NO-LENGTH", "/api/images", b"x",
                         ("Content-Type: application/octet-stream",), 411)),
        ("写入媒体类型错误", ("POST", route + "/decisions", b"x",
                            {"Content-Type": "text/plain"}, 415)),
        ("JSON 体损坏", ("POST", "/api/plans", b"{",
                        {"Content-Type": "application/json"}, 400)),
        ("未知计划", ("GET", f"/api/plans/{'a' * 32}", None, {}, 404)),
        ("未知路由", ("GET", "/api/nope", None, {}, 404)),
        ("查询参数过多", ("GET", "/?" + "&".join(f"k{i}=1" for i in range(20)),
                        None, {}, 400)),
        ("主机头伪造", ("HOST", "/", None, {}, 403)),
        ("跨站 Sec-Fetch-Site", ("POST", route + "/archive",
                                  {"revision": 2},
                                  {"Sec-Fetch-Site": "cross-site",
                                   "Content-Type": "application/json"}, 403)),
        ("跨源 Origin", ("POST", route + "/archive", {"revision": 2},
                         {"Origin": "https://evil.example",
                          "Content-Type": "application/json"}, 403)),
        # 两栈都返回 501 text/html；body 不同（旧栈是 http.server 通用错误页，
        # 新栈是应用统一 HTML Problem 页），矩阵只断言状态码一致。
        ("不支持的方法", ("METHOD", "/", None, {}, 501)),
    ]
    rows = []
    for label, (kind, target, payload_body, extra, expected) in cases:
        if kind in ("RAW-NO-LENGTH", "METHOD"):
            method = "DELETE" if kind == "METHOD" else "POST"
            status, body, heads = stack.client.raw(
                method, target, payload_body if kind != "METHOD" else b"",
                extra_headers=extra if kind == "RAW-NO-LENGTH" else (),
                send_body=kind != "RAW-NO-LENGTH")
            content_type = ""
            for line in heads.split("\r\n"):
                if line.lower().startswith("content-type:"):
                    content_type = line.split(":", 1)[1].strip()
        elif kind == "HOST":
            # http.client 会强制改写 Host，这里用裸报文发送伪造主机名。
            status, body, heads = stack.client.raw(
                "GET", target, b"", extra_headers=("Host: evil.example:8745",),
                send_body=False)
            content_type = ""
            for line in heads.split("\r\n"):
                if line.lower().startswith("content-type:"):
                    content_type = line.split(":", 1)[1].strip()
        else:
            response = stack.call(target, payload_body, status=None, headers=extra)
            status, body, content_type = response["status"], response["body"], response["kind"]
        code = json.loads(body)["code"] if body[:1] == b"{" else None
        rows.append({"label": label, "status": status,
                     "kind": content_type.split(";", 1)[0],
                     "digest": stable_digest(body), "code": code})
        fail(status == expected, f"{stack.name} {label}: {status} != {expected}")
    return rows


# ------------------------------------------------- 上限附近的 SQLite 请求

class _FakeHeaders:
    def __init__(self, content_length):
        self._length = content_length

    def get_all(self, name, failobj=None):
        if name.lower() == "content-length" and self._length is not None:
            return [str(self._length)]
        return failobj


class _FakeHandler:
    """同时满足旧 Handler 与新 WSGI Request 的 inputs.payload 读取约定。"""

    def __init__(self, content_length, body):
        self.headers = _FakeHeaders(content_length)
        self.rfile = io.BytesIO(body)


def shared_payload_constraints():
    """直接验证两栈共用的 inputs.payload 长度/截断约束，与承载服务器无关。"""
    from pagefold.errors import Problem
    from pagefold.inputs import payload

    outcomes = []

    def probe(label, length, body, limit=1024):
        handler = _FakeHandler(length, body)
        try:
            data = payload(handler, limit)
            outcomes.append((label, 200, "ok", len(data)))
        except Problem as exc:
            outcomes.append((label, exc.status, exc.code, len(body)))

    probe("缺少 Content-Length", None, b"x")
    probe("声明 9 字节仅收到 1 字节", 9, b"x")
    probe("超过上限 413", 2048, b"y" * 2048)
    probe("完整读取", 4, b"abcd")
    expected = {"缺少 Content-Length": (411, "content_length"),
                "声明 9 字节仅收到 1 字节": (400, "request_incomplete"),
                "超过上限 413": (413, "request_size"),
                "完整读取": (200, "ok")}
    for label, status, code, _ in outcomes:
        fail((status, code) == expected[label],
             f"共享 payload 约束 {label} 异常：{status}/{code}")
    return outcomes


def size_limits(stack, near_limit):
    rows = []
    port = stack.server.server_port
    host = f"127.0.0.1:{port}"
    limit = 4 * 1024 * 1024
    ok = stack.call("/api/images", near_limit, 201, {
        "Content-Type": "application/octet-stream", "X-Image-Name": "near-limit"})
    rows.append(("逼近 4 MiB 的合法镜像", ok["status"], ok["json"]["id"], len(near_limit)))
    oversized = b"SQLite format 3\x00" + b"\x00" * (limit + 16)
    over = tolerant_upload(port, host, "/api/images", oversized)
    fail(over["status"] == 413, f"超限镜像应返回 413，实际 {over['status']}")
    rows.append(("超过 4 MiB 上限", over["status"], over["json"]["code"], limit + 32))
    exact = b"SQLite format 3\x00" + b"\x00" * (limit - 16)
    at_limit = tolerant_upload(port, host, "/api/images", exact)
    fail(at_limit["status"] == 422, f"满 4 MiB 非法结构应返回 422，实际 {at_limit['status']}")
    rows.append(("恰为 4 MiB 但结构非法", at_limit["status"], at_limit["json"]["code"], limit))
    body_over = b"{" + b" " * 32768
    form_over = tolerant_upload(port, host, "/api/plans", body_over,
                                content_type="application/json")
    fail(form_over["status"] == 413, f"超限 JSON 应返回 413，实际 {form_over['status']}")
    rows.append(("JSON 写入超过 32 KiB", form_over["status"], form_over["json"]["code"],
                 len(body_over)))
    return rows


# ------------------------------------------------- 多 worker 并发同一计划

def concurrency(stack):
    s = stack.state
    sources = {"base": s["base"]["id"], "left": s["left"]["id"], "right": s["right"]["id"]}
    plan = stack.call("/api/plans", sources, 201)["json"]
    route = f"/api/plans/{plan['id']}"
    conflict = next(item for item in plan["items"] if not item["automatic"])
    barrier = threading.Barrier(4)
    results = []

    def decide(choice):
        barrier.wait()
        response = stack.call(route + "/decisions",
                              {"item": conflict["id"], "choice": choice, "revision": 0},
                              status=None)
        results.append((choice, response["status"],
                        response["json"].get("revision") if response["json"] else None,
                        response["json"].get("code") if response["json"] else None))

    # 四个 worker 都基于 revision=0 同时写入；left/right/base 都会改变裁决，
    # clear 是保持未决的幂等空操作（不推进版本）。
    threads = [threading.Thread(target=decide, args=(role,))
               for role in ("left", "right", "base", "clear")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(row[1] for row in results)
    advanced = [row for row in results if row[1] == 200 and row[2] == 1]
    stale = [row for row in results if row[1] == 409]
    noop = [row for row in results if row[1] == 200 and row[2] == 0]
    # 关键不变量：跨 worker 只有一个写入能把版本从 0 推进到 1；其它变更写入
    # 全部以 stale_revision(409) 拒绝；clear 若先于胜出者执行则是返回 200 但
    # 不推进版本的幂等空操作，若后执行则同样收到 409，两种调度都合法。
    fail(len(advanced) == 1, f"应恰好一个写入推进版本：{results}")
    fail(all(row[3] == "stale_revision" for row in stale), f"过期写入代码异常：{results}")
    fail(all(row[0] == "clear" and row[2] == 0 for row in noop),
         f"非推进的 200 只能是不改变版本的 clear：{results}")
    fail(len(advanced) + len(stale) + len(noop) == 4, f"并发裁决计数异常：{results}")
    final = stack.call(route)["json"]
    fail(final["revision"] == 1, "并发后计划版本应为 1")

    # 同版本并发重演：证据复用，两个 worker 拿到同一条候选摘要。
    barrier2 = threading.Barrier(2)
    replays = []

    def rehearse():
        barrier2.wait()
        replays.append(stack.call(route + "/rehearse", {"revision": 1})["json"])

    workers = [threading.Thread(target=rehearse) for _ in range(2)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    fail(len({r["candidate_digest"] for r in replays}) == 1, "并发重演候选摘要不一致")
    # 可跨栈比较的语义签名：恰有一个写入推进版本，其余三个请求不推进（过期 409
    # 或 clear 幂等空操作，具体比例随线程调度而变），最终版本为 1，且同栈内
    # 并发重演收敛到同一候选。胜出者相关的具体摘要不跨栈比较。
    signature = {
        "advanced": len(advanced), "non_advancing": len(stale) + len(noop),
        "final_revision": final["revision"],
        "replay_converged": len({r["candidate_digest"] for r in replays}) == 1}
    return {"decision_races": results, "statuses": statuses,
            "replay_digests": [r["candidate_digest"] for r in replays],
            "signature": signature}


def forced_busy(root):
    """用零超时 Store 确定性触发 SQLITE_BUSY→503 映射，不依赖计时。"""
    from pagefold.wsgi import make_app

    class FastStore(Store):
        def connect(self, write=False, snapshot=True):
            db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write or snapshot:
                # 演练专用：连只读快照也立即取写锁，使外部持锁时确定性地 BUSY。
                db.execute("BEGIN IMMEDIATE")
            return _Autocommit(db)

    class _Autocommit:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            return self.db

        def __exit__(self, *exc):
            self.db.close()

    state = Path(root) / "busy.sqlite"
    store = FastStore(state)
    holder = sqlite3.connect(state, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        app = make_app(store, allowed_hosts=["test:80"])
        # 选 /api/ 路径，确保繁忙时返回 JSON Problem（HTML 路径按设计返回错误页）。
        environ = {
            "REQUEST_METHOD": "GET", "PATH_INFO": "/api/plans", "SCRIPT_NAME": "",
            "QUERY_STRING": "", "SERVER_NAME": "test", "SERVER_PORT": "80",
            "HTTP_HOST": "test:80", "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(), "wsgi.url_scheme": "http",
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(headers)

        body = b"".join(app(environ, start_response))
        fail(captured["status"] == 503, f"繁忙状态码应为 503，实际 {captured['status']}")
        problem = json.loads(body)
        fail(problem["code"] == "state_busy", f"错误代码异常：{problem}")
        return captured["status"], problem["code"]
    finally:
        holder.close()


# ------------------------------------------------- 代理转发与表单回跳

class Proxy:
    """最小反向代理：保留浏览器原始 Host/Origin，转发到上游 WSGI 服务。"""

    def __init__(self, upstream_port, host):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_one_request(self):
                raw = self.rfile.readline()
                if not raw:
                    return
                method, target, _ = raw.split(b" ", 2)
                headers = []
                length = 0
                while True:
                    line = self.rfile.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    name, _, value = line.decode("iso-8859-1").partition(":")
                    value = value.strip()
                    if name.lower() == "content-length":
                        length = int(value)
                    headers.append((name, value))
                payload_body = self.rfile.read(length) if length else b""
                upstream = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=15)
                upstream.request(method.decode(), target.decode(), payload_body,
                                 dict(headers))
                response = upstream.getresponse()
                data = response.read()
                upstream.close()
                self.wfile.write(
                    (f"HTTP/1.1 {response.status} {response.reason}\r\n").encode())
                for key, value in response.getheaders():
                    if key.lower() not in ("connection", "transfer-encoding",
                                           "content-length", "server", "date"):
                        self.wfile.write(f"{key}: {value}\r\n".encode())
                self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                self.wfile.write(data)
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.host = host
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def stop(self):
        self.server.shutdown()


def proxy_redirects(wsgi_stack):
    port = wsgi_stack.server.server_port
    proxy_host = f"proxy.local:{port}"
    proxy = Proxy(port, proxy_host)
    # 新 WSGI 服务把代理主机名加入可信名单（与旧约束相同的显式 Host 白名单）。
    wsgi_stack.server.allowed_hosts.append(proxy_host)
    wsgi_stack.server.set_app(make_app(
        wsgi_stack.server.store, allowed_hosts=wsgi_stack.server.allowed_hosts))
    try:
        client = Client(proxy.port, host=proxy_host)
        response = client.call("/demo", b"", headers={"Content-Length": "0"})
        fail(response["status"] == 303, f"表单回跳应为 303，实际 {response['status']}")
        fail(response["location"].startswith("/plans/"),
             f"回跳地址异常：{response['location']}")
        followed = client.call(response["location"])
        fail(followed["status"] == 200 and "主键行差异".encode() in followed["body"],
             "回跳后的计划页无法渲染")
        spoofed = Client(proxy.port, host="evil.example:80").call("/")
        fail(spoofed["status"] == 403, "伪造 Host 应被白名单拒绝")

        # SCRIPT_NAME 前缀挂载：验证 WSGI 原生前缀拼接（旧协议无此能力）。
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/demo", "SCRIPT_NAME": "/pf",
            "QUERY_STRING": "", "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": str(port), "HTTP_HOST": f"127.0.0.1:{port}",
            "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(), "wsgi.url_scheme": "http",
        }
        seen = {}

        def start_response(status, headers):
            seen["status"] = int(status.split(" ", 1)[0])
            seen["headers"] = dict(headers)

        list(wsgi_stack.server.application(environ, start_response))
        fail(seen["status"] == 303 and seen["headers"]["Location"].startswith("/pf/plans/"),
             f"前缀挂载回跳异常：{seen}")
        return {"plain_redirect": response["location"],
                "prefixed_redirect": seen["headers"]["Location"],
                "spoof_status": spoofed["status"]}
    finally:
        proxy.stop()


# ---------------------------------------------------------------- 主流程

def compare_probes(label, legacy, wsgi):
    mismatches = []
    for left, right in zip(legacy, wsgi):
        if left["label"] != right["label"]:
            mismatches.append(f"{label}: 步骤错位 {left['label']} != {right['label']}")
            continue
        for field in ("status", "kind", "digest"):
            if left[field] != right[field]:
                mismatches.append(
                    f"{left['label']}: {field} {left[field]} != {right[field]}")
    return mismatches


def main():
    parser = argparse.ArgumentParser(description="HTTP→WSGI 迁移对照演练")
    parser.add_argument("--report", type=Path, default=None, help="把对照结果写入 JSON 文件")
    args = parser.parse_args()
    report = {"stacks": {}}
    mismatches = []

    with tempfile.TemporaryDirectory(prefix="pagefold-wsgi-drill-") as root:
        root = Path(root)
        legacy_server = make_legacy_server(Store(root / "legacy.sqlite"), 0)
        wsgi_server = make_wsgi_server(Store(root / "wsgi.sqlite"), 0)
        legacy, wsgi = Stack("legacy-http", legacy_server), Stack("new-wsgi", wsgi_server)
        threads = [start(legacy_server), start(wsgi_server)]
        try:
            near_limit = near_limit_image()

            legacy_probes, legacy_summary = workflow(legacy)
            wsgi_probes, wsgi_summary = workflow(wsgi)
            report["workflow"] = {"legacy": legacy_probes, "wsgi": wsgi_probes}
            mismatches += compare_probes("工作流", legacy_probes, wsgi_probes)

            print("== 1. 同构工作流逐跳对照（状态 / 内容类型 / 归一化摘要） ==")
            print(f"{'步骤':<22}{'旧协议':>10}{'新 WSGI':>12}{'摘要一致':>10}")
            for left, right in zip(legacy_probes, wsgi_probes):
                same = "✓" if left["digest"] == right["digest"] and left["status"] == right["status"] else "✗"
                print(f"{left['label']:<22}{left['status']:>10}{right['status']:>12}{same:>10}")

            print("\n== 2. 版本 / 来源 / 内容摘要 ==")
            for key in ("sources", "candidate_digest", "candidate_size", "schema_signature"):
                same = legacy_summary[key] == wsgi_summary[key]
                mismatches += [] if same else [f"摘要字段 {key} 不一致"]
                print(f"{key:<20} 旧={legacy_summary[key]} 新={wsgi_summary[key]} {'✓' if same else '✗'}")
            legacy_rev = [p for p in legacy_probes if "版本" in p["label"] or "重演" in p["label"]
                          or "封存" in p["label"] or "归档" in p["label"]]
            print("版本推进轨迹两栈一致：",
                  "✓" if [(p["label"], p["status"]) for p in legacy_rev] ==
                  [(p["label"], p["status"]) for p in
                   [q for q in wsgi_probes if q["label"] in {p["label"] for p in legacy_rev}]]
                  else "✗")
            report["summary"] = {"legacy": legacy_summary, "wsgi": wsgi_summary}
            print("入口标记：旧协议 X-Pagefold-Entry =",
                  next((p["entry"] for p in legacy_probes if p["entry"] is not None), "（无）"),
                  "；新协议 =",
                  next(p["entry"] for p in wsgi_probes if p["entry"] is not None))

            print("\n== 3. 失败语义矩阵 ==")
            legacy_failures, wsgi_failures = failure_matrix(legacy), failure_matrix(wsgi)
            report["failures"] = {"legacy": legacy_failures, "wsgi": wsgi_failures}
            print(f"{'场景':<24}{'旧状态/代码':>16}{'新状态/代码':>16}{'一致':>8}")
            for left, right in zip(legacy_failures, wsgi_failures):
                same = (left["status"] == right["status"] and left["code"] == right["code"])
                if not same:
                    mismatches.append(f"失败语义 {left['label']} 不一致")
                print(f"{left['label']:<24}{str(left['status']) + '/' + str(left['code']):>16}"
                      f"{str(right['status']) + '/' + str(right['code']):>16}{'✓' if same else '✗':>8}")
            shared = shared_payload_constraints()
            report["shared_payload_constraints"] = shared
            print("共享 inputs.payload 约束（两栈同模块，与承载服务器无关）：")
            for label, status, code, _ in shared:
                print(f"  {label:<26} {status}/{code} ✓")

            print("\n== 4. 上限附近的 SQLite 请求（原约束：4 MiB / 32 KiB） ==")
            legacy_sizes, wsgi_sizes = size_limits(legacy, near_limit), size_limits(wsgi, near_limit)
            report["size_limits"] = {"legacy": legacy_sizes, "wsgi": wsgi_sizes}
            for left, right in zip(legacy_sizes, wsgi_sizes):
                same = left[1] == right[1] and left[2] == right[2]
                if not same:
                    mismatches.append(f"上限场景 {left[0]} 不一致")
                print(f"{left[0]:<26} 旧={left[1]}/{left[2]} 新={right[1]}/{right[2]} "
                      f"({'✓' if same else '✗'})")

            print("\n== 5. 多 worker 同时操作同一计划 ==")
            legacy_conc, wsgi_conc = concurrency(legacy), concurrency(wsgi)
            report["concurrency"] = {"legacy": legacy_conc, "wsgi": wsgi_conc}
            same_conc = (legacy_conc["signature"] == wsgi_conc["signature"])
            if not same_conc:
                mismatches.append("并发语义不一致")
            print("并发裁决语义签名：旧", legacy_conc["signature"])
            print("                  新", wsgi_conc["signature"], "✓" if same_conc else "✗")
            print("  恰有一个写入者推进 revision=1，其余 stale_revision 409（clear 幂等空操作除外）；"
                  "并发重演候选摘要相同。")
            busy_status, busy_code = forced_busy(root)
            report["busy"] = {"status": busy_status, "code": busy_code}
            print(f"SQLITE_BUSY 映射：{busy_status}/{busy_code} ✓")

            print("\n== 6. 代理转发后的浏览器表单回跳 ==")
            proxy_result = proxy_redirects(wsgi)
            report["proxy"] = proxy_result
            print("经反向代理 POST /demo →", proxy_result["plain_redirect"],
                  f"({303} 见下) ✓")
            print("前缀挂载 SCRIPT_NAME=/pf →", proxy_result["prefixed_redirect"], "✓")
            print("伪造 Host 经代理 →", proxy_result["spoof_status"], "✓")
        finally:
            legacy_server.shutdown()
            wsgi_server.shutdown()
            for thread in threads:
                thread.join()

    report["mismatches"] = mismatches
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"\n对照报告已写入 {args.report}")
    if mismatches:
        print("\n迁移对照发现不一致：")
        for item in mismatches:
            print(" -", item)
        raise SystemExit(1)
    print("\n全部对照一致：WSGI 入口保留了旧协议的版本、来源、摘要与失败语义。")


if __name__ == "__main__":
    main()
