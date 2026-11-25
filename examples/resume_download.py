"""实测封存 SQLite 的中断续领与混用凭证拒绝。

通过真实 HTTP 入口验证：
1. 下载在两个不同字节位置中断后，凭 Range 续领拼成完整文件；
2. 续领前用 ETag（凭证候选摘要）确认手中部分文件与服务端候选是同一份，
   混用另一份成果的凭证会被拒绝（412）或整份重置（If-Range 失配 -> 200）；
3. 计划在传输完成前归档，已封存字节仍可续领；
4. 最终文件可以作为独立 SQLite 打开，且 SHA-256 与凭证中的候选摘要一致。

用法（从仓库根目录）：
    python3 -I examples/resume_download.py all
    python3 -I examples/resume_download.py interrupt|mixed|archive
"""

import argparse
import hashlib
import http.client
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

SCENARIOS = ("interrupt", "mixed", "archive")


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


class Client:
    def __init__(self, port):
        self.port = port

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def call(self, route, method="GET", headers=None, body=None):
        conn = self.connect()
        try:
            conn.request(method, route, body=body, headers=headers or {})
            result = conn.getresponse()
            payload = result.read()
            return result.status, dict(result.headers), payload
        finally:
            conn.close()

    def stream(self, route, headers=None):
        conn = self.connect()
        conn.request("GET", route, headers=headers or {})
        result = conn.getresponse()
        return conn, result

    def json(self, route, method="GET", data=None):
        headers = {}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        status, _, payload = self.call(route, method, headers, body)
        expect(status < 400, f"{route} 返回 {status}: {payload[:200]!r}")
        return json.loads(payload)

    def upload(self, name):
        status, _, payload = self.call(
            "/api/images", "POST",
            {"X-Image-Name": name, "Content-Type": "application/octet-stream"}, image(name))
        expect(status == 201, f"登记 {name} 失败：{status} {payload[:200]!r}")
        return json.loads(payload)

    def post(self, route, data):
        return self.json(route, "POST", data)


def sealed_plan(client, conflict_choice):
    """上传示例镜像、裁决唯一冲突并封存，返回 (plan_id, 凭证)。"""
    base = client.upload("quartz")
    left = client.upload("cerulean")
    right = client.upload("umber")
    plan = client.json("/api/plans", "POST",
                       {"base": base["id"], "left": left["id"], "right": right["id"]})
    conflict = next(item for item in plan["items"] if not item["automatic"])
    decided = client.post(f"/api/plans/{plan['id']}/decisions",
                          {"item": conflict["id"], "choice": conflict_choice, "revision": 0})
    client.post(f"/api/plans/{plan['id']}/rehearse", {"revision": decided["revision"]})
    client.post(f"/api/plans/{plan['id']}/seal", {"revision": decided["revision"]})
    certificate = client.json(f"/api/plans/{plan['id']}/certificate")
    return plan["id"], certificate


def verify_sqlite(path, certificate, label):
    data = Path(path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    expect(actual == certificate["candidate_digest"],
           f"{label}: 摘要 {actual} 与凭证 {certificate['candidate_digest']} 不符")
    expect(len(data) == certificate["candidate_size"], f"{label}: 文件大小与凭证不符")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        expect(db.execute("PRAGMA integrity_check").fetchone() == ("ok",), f"{label}: 完整性检查失败")
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], f"{label}: 存在外键违规")
        expect(db.execute("SELECT COUNT(*) FROM node").fetchone()[0] >= 2, f"{label}: 数据表不可查询")
    finally:
        db.close()


def scenario_interrupt(client, plan_id, certificate, workdir):
    """两次真实中断后续领，最终拼成完整、可打开的 SQLite。"""
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "interrupted.sqlite"
    route = f"/api/plans/{plan_id}/download"
    size = certificate["candidate_size"]
    first_cut = size // 3
    second_cut = size * 2 // 3

    # 第一次连接：只读前一段就主动断开，模拟传输中断。
    conn, response = client.stream(route)
    try:
        expect(response.status == 200, f"首次下载状态码 {response.status}")
        expect(response.getheader("Accept-Ranges") == "bytes", "未声明 Accept-Ranges")
        expect(response.getheader("ETag") == f'"{certificate["candidate_digest"]}"', "ETag 未绑定凭证摘要")
        target.write_bytes(response.read(first_cut))
    finally:
        conn.close()
    expect(target.stat().st_size == first_cut, "中断后本地部分文件大小不符")

    # 第二次连接：从断点续领一段，再中断一次。
    conn, response = client.stream(route, {"Range": f"bytes={first_cut}-",
                                           "If-Range": f'"{certificate["candidate_digest"]}"'})
    try:
        expect(response.status == 206, f"续领状态码 {response.status}")
        expect(response.getheader("Content-Range") == f"bytes {first_cut}-{size - 1}/{size}",
               "Content-Range 不正确")
        with target.open("ab") as handle:
            handle.write(response.read(second_cut - first_cut))
    finally:
        conn.close()
    expect(target.stat().st_size == second_cut, "第二次中断后本地大小不符")

    # 第三次连接：领走余下字节并拼接。
    status, headers, tail = client.call(route, headers={"Range": f"bytes={second_cut}-"})
    expect(status == 206 and headers["Content-Range"] == f"bytes {second_cut}-{size - 1}/{size}",
           f"末次续领异常：{status} {headers.get('Content-Range')}")
    with target.open("ab") as handle:
        handle.write(tail)
    expect(target.stat().st_size == size, "拼接后大小与凭证不一致")
    verify_sqlite(target, certificate, "中断续领")

    # 后缀区间同样可用。
    suffix = 64
    status, headers, payload = client.call(route, headers={"Range": f"bytes=-{suffix}"})
    expect(status == 206 and len(payload) == suffix, "后缀区间续领失败")
    print(f"interrupt ✓ 两次中断后按 Range 续领完成，{size} 字节，SQLite 可打开且摘要等于凭证")


def scenario_mixed(client, plan_a, cert_a, plan_b, cert_b, workdir):
    """手里是另一份成果的部分文件时，续领必须被拒绝或整份重置。"""
    workdir.mkdir(parents=True, exist_ok=True)
    route_a = f"/api/plans/{plan_a}/download"
    target = workdir / "mixed.sqlite"
    cut = cert_a["candidate_size"] // 2

    # 前端误把成果 B 的前半截当作成果 A 的部分文件。
    _, _, candidate_b = client.call(f"/api/plans/{plan_b}/download")
    foreign_prefix = candidate_b[:cut]
    target.write_bytes(foreign_prefix)
    foreign_etag = f'"{cert_b["candidate_digest"]}"'
    own_etag = f'"{cert_a["candidate_digest"]}"'
    expect(cert_a["candidate_digest"] != cert_b["candidate_digest"], "两份候选摘要不应相同")

    # 1) 拿着 B 的凭证走 If-Match：预检失败，服务端拒绝，本地文件不得被改动。
    before = target.read_bytes()
    status, headers, payload = client.call(
        route_a, headers={"Range": f"bytes={cut}-", "If-Match": foreign_etag})
    expect(status == 412, f"混用凭证未被拒绝，状态码 {status}")
    expect(headers.get("ETag") == own_etag, "412 响应未回传当前候选 ETag")
    expect(payload == b"" and target.read_bytes() == before, "拒绝续领时本地部分文件被改写")

    # 2) 代理把 B 的校验器塞进 If-Range：候选不一致，整份重置（200）而非错误拼接。
    status, headers, full_a = client.call(
        route_a, headers={"Range": f"bytes={cut}-", "If-Range": foreign_etag})
    expect(status == 200 and len(full_a) == cert_a["candidate_size"],
           f"If-Range 失配未整份重置：{status}")
    expect(hashlib.sha256(full_a).hexdigest() == cert_a["candidate_digest"], "重置内容不是当前候选")

    # 3) 两份候选存在逐字节分歧的位置；对该位置发 Range 请求，取回的是 A 自己的字节，
    #    证明服务端始终按当前候选应答，部分文件不能靠盲拼通过校验。
    divergence = next(i for i, (x, y) in enumerate(zip(candidate_b, full_a)) if x != y)
    status, headers, chunk = client.call(
        route_a, headers={"Range": f"bytes={divergence}-{divergence}", "If-Match": own_etag})
    expect(status == 206 and chunk == full_a[divergence:divergence + 1]
           and chunk != candidate_b[divergence:divergence + 1], "分歧位置未按当前候选返回字节")
    target.write_bytes(full_a)
    verify_sqlite(target, cert_a, "混用凭证后重置")

    # 4) 条件重验：If-None-Match 命中返回 304，不重复传输字节。
    status, headers, payload = client.call(route_a, headers={"If-None-Match": own_etag})
    expect(status == 304 and payload == b"", f"条件重验异常：{status}")
    expect(headers.get("ETag") == own_etag, "304 未回传 ETag")

    # 5) 无法满足的区间返回 416 并说明当前文件总长度。
    status, headers, payload = client.call(
        route_a, headers={"Range": f"bytes={cert_a['candidate_size']}-"})
    expect(status == 416, f"越界区间未被拒绝：{status}")
    expect(headers.get("Content-Range") == f"bytes */{cert_a['candidate_size']}", "416 缺少总长度")
    print("mixed ✓ 混用凭证被 412 拒绝且本地不变；If-Range 失配整份重置；304/416 语义正确")


def scenario_archive(client, plan_id, certificate, revision, workdir):
    """传输完成前归档计划，已封存成果仍可续领到完整文件。"""
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "archived.sqlite"
    route = f"/api/plans/{plan_id}/download"
    cut = certificate["candidate_size"] // 2

    conn, response = client.stream(route)
    try:
        expect(response.status == 200, "归档前首次下载失败")
        target.write_bytes(response.read(cut))
    finally:
        conn.close()

    archived = client.post(f"/api/plans/{plan_id}/archive", {"revision": revision})
    expect(archived["state"] == "archived", "计划未进入归档状态")

    status, headers, tail = client.call(route, headers={"Range": f"bytes={cut}-"})
    expect(status == 206, f"归档后续领被拒绝：{status}")
    target.write_bytes(target.read_bytes() + tail)
    verify_sqlite(target, certificate, "归档后续领")
    print("archive ✓ 传输中归档不影响已封存字节，续领文件仍可打开且摘要一致")


def main():
    parser = argparse.ArgumentParser(description="实测封存 SQLite 的中断续领与凭证校验")
    parser.add_argument("scenario", choices=("all", *SCENARIOS))
    choice = parser.parse_args().scenario
    with tempfile.TemporaryDirectory(prefix="pagefold-resume-") as root:
        root = Path(root)
        work_dir = root / "downloads"
        work_dir.mkdir()
        with make_server(Store(root / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                plan_a, cert_a = sealed_plan(client, "right")
                plan_b, cert_b = sealed_plan(client, "base")
                if choice in ("all", "interrupt"):
                    scenario_interrupt(client, plan_a, cert_a, work_dir / "interrupt")
                if choice in ("all", "mixed"):
                    scenario_mixed(client, plan_a, cert_a, plan_b, cert_b, work_dir / "mixed")
                if choice in ("all", "archive"):
                    scenario_archive(client, plan_a, cert_a, cert_a["revision"],
                                     work_dir / "archive")
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
