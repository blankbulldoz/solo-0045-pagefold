#!/usr/bin/env python3
"""End-to-end upgrade/drain/recovery demonstration for Pagefold.

This script is intentionally dependency-free.  It creates two isolated local
state directories and demonstrates:

1. write-gate + SIGTERM drain while an accepted rehearsal holds the SQLite
   write lock and a slow client is downloading a sealed ~3 MiB candidate;
2. SIGKILL during the same locked rehearsal/download, followed by SQLite WAL
   recovery and continuation of the open plan.

Run from a clean checkout:

    python3 deploy/demo_upgrade.py
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pagefold.demo import image as demo_image  # noqa: E402

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def log(message, color=CYAN):
    print(f"{color}{message}{RESET}", flush=True)


def ok(message):
    print(f"{GREEN}  ✓ {message}{RESET}", flush=True)


def warn(message):
    print(f"{YELLOW}  ! {message}{RESET}", flush=True)


def fail(message):
    print(f"{RED}  ✗ {message}{RESET}", flush=True)


class CheckFailure(RuntimeError):
    pass


def expect(condition, message):
    if not condition:
        raise CheckFailure(message)
    ok(message)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_request(port, method, path, body=None, headers=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    request_headers = dict(headers or {})
    try:
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        data = response.read()
        result_headers = {k.lower(): v for k, v in response.getheaders()}
        return response.status, data, result_headers
    finally:
        conn.close()


def api(port, method, path, payload=None, expected=200, headers=None, timeout=30):
    body = None
    request_headers = dict(headers or {})
    if payload is not None and not isinstance(payload, (bytes, bytearray)):
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    elif payload is not None:
        body = bytes(payload)
    status, data, _ = http_request(port, method, path, body, request_headers, timeout)
    try:
        parsed = json.loads(data.decode("utf-8")) if data else None
    except Exception:
        parsed = {"_raw": data[:500].decode("utf-8", "replace")}
    if status != expected:
        raise CheckFailure(f"{method} {path}: expected {expected}, got {status}: {data[:500]!r}")
    return parsed


def wait_health(port, proc, log_path, timeout=25):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-4000:]
            raise CheckFailure(f"server exited early with {proc.returncode}\n{tail}")
        try:
            status, data, _ = http_request(port, "GET", "/healthz", timeout=2)
            if status == 200:
                return json.loads(data)
        except Exception as exc:
            last_error = exc
            time.sleep(0.15)
    raise CheckFailure(f"server did not become healthy: {last_error}")


def wait_status(port, **expected):
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        last = api(port, "GET", "/-/status")
        if all(last.get(key) == value for key, value in expected.items()):
            return last
        time.sleep(0.1)
    raise CheckFailure(f"status did not reach {expected}; last={last}")


def start_server(workdir, scenario, *, slow=False, drain_timeout=20):
    directory = workdir / scenario
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / "state.sqlite"
    log_path = directory / "server.log"
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    args = [sys.executable]
    if slow:
        gate = directory / "rehearse-delay.seconds"
        marker = directory / "write-lock.marker"
        seal_gate = directory / "seal-delay.seconds"
        seal_marker = directory / "seal-lock.marker"
        env["PAGEFOLD_DEMO_REHEARSE_GATE"] = str(gate)
        env["PAGEFOLD_DEMO_LOCK_MARKER"] = str(marker)
        env["PAGEFOLD_DEMO_SEAL_GATE"] = str(seal_gate)
        env["PAGEFOLD_DEMO_SEAL_MARKER"] = str(seal_marker)
        args += [str(ROOT / "deploy" / "slow_demo_server.py")]
    else:
        args += ["-m", "pagefold"]
    args += ["--state", str(state), "--port", str(port), "--drain-timeout", str(drain_timeout)]
    log_handle = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log_handle,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_health(port, proc, log_path)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    return {"proc": proc, "port": port, "dir": directory, "state": state,
            "log": log_path, "gate": directory / "rehearse-delay.seconds",
            "marker": directory / "write-lock.marker",
            "seal_gate": directory / "seal-delay.seconds",
            "seal_marker": directory / "seal-lock.marker"}


def stop_server(context, timeout=25):
    proc = context["proc"]
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise


def upload(port, data, name):
    return api(port, "POST", "/api/images", data, 201, {
        "Content-Type": "application/octet-stream",
        "X-Image-Name": name,
    })


def seed_open_plan_with_history(port):
    base = upload(port, demo_image("quartz"), "quartz.sqlite")
    left = upload(port, demo_image("cerulean"), "cerulean.sqlite")
    right = upload(port, demo_image("umber"), "umber.sqlite")
    sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
    plan = api(port, "POST", "/api/plans", sources, 201)
    plan_id = plan["id"]
    conflict = next(item for item in plan["items"] if item["automatic"] is None)
    decision = {"revision": 0, "item": conflict["id"], "choice": "right"}
    first = api(port, "POST", f"/api/plans/{plan_id}/decisions", decision)
    expect(first["revision"] == 1, "开放计划首次裁决推进到 revision=1")
    passed = api(port, "POST", f"/api/plans/{plan_id}/rehearse", {"revision": 1})
    expect(passed["status"] == "passed", "预置 revision=1 重演通过并形成历史")
    changed = api(port, "POST", f"/api/plans/{plan_id}/decisions",
                  {**decision, "revision": 1, "choice": "base"})
    expect(changed["revision"] == 2, "第二次裁决推进到 revision=2，制造待重演版本")
    return {"id": plan_id, "conflict": conflict["id"], "sources": sources}


def make_large_image():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA page_size=4096")
        con.execute("CREATE TABLE bulk(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
        rows = []
        for i in range(60):
            # Deterministic 64 KiB incompressible-looking chunks; total remains
            # below Pagefold's 4 MiB input limit.
            chunk = hashlib.sha256(f"pagefold-bulk-{i}".encode()).digest() * 2048
            rows.append((i, chunk[:65536]))
        con.executemany("INSERT INTO bulk VALUES(?, ?)", rows)
        con.commit()
        data = con.serialize()
        if len(data) > 4 * 1024 * 1024:
            raise CheckFailure("large fixture exceeds Pagefold image limit")
        return data
    finally:
        con.close()


def seed_large_sealed_release(port):
    image_id = upload(port, make_large_image(), "large-bulk.sqlite")["id"]
    plan = api(port, "POST", "/api/plans",
               {"base": image_id, "left": image_id, "right": image_id}, 201)
    plan_id = plan["id"]
    attempt = api(port, "POST", f"/api/plans/{plan_id}/rehearse", {"revision": 0})
    expect(attempt["status"] == "passed", "大文件候选重演通过")
    certificate = api(port, "POST", f"/api/plans/{plan_id}/seal", {"revision": 0})
    expect(certificate["candidate_digest"] == attempt["candidate_digest"], "大文件凭证绑定候选摘要")
    api(port, "POST", f"/api/plans/{plan_id}/archive", {"revision": 0})
    archived = api(port, "GET", f"/api/plans/{plan_id}")
    expect(archived["state"] == "archived", "大文件计划同时处于 archived，验证归档后阅读资格独立")
    return {"id": plan_id, "certificate": certificate,
            "sha256": certificate["candidate_digest"], "size": certificate["candidate_size"]}


def slow_download(port, plan_id, destination, pause=0.08):
    result = {"completed": False, "bytes": 0, "sha256": None, "error": None, "path": str(destination)}
    digest = hashlib.sha256()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    # Keep the receive window small so the server's write remains blocked and
    # lifecycle active_downloads stays 1 while this user is "downloading".
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    except OSError:
        pass
    conn.sock = sock
    try:
        conn.request("GET", f"/api/plans/{plan_id}/download")
        response = conn.getresponse()
        if response.status != 200:
            result["error"] = f"HTTP {response.status}: {response.read()[:200]!r}"
            return result
        expected_length = int(response.getheader("Content-Length") or "0")
        result["expected_bytes"] = expected_length
        with destination.open("wb") as output:
            while True:
                chunk = response.read(32768)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                result["bytes"] += len(chunk)
                time.sleep(pause)
        result["sha256"] = digest.hexdigest()
        if expected_length and result["bytes"] != expected_length:
            result["error"] = f"incomplete download: {result['bytes']}/{expected_length} bytes"
        else:
            result["completed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["sha256"] = digest.hexdigest() if result["bytes"] else None
    finally:
        conn.close()
    return result


def start_download(context, release, name, pause=0.08):
    destination = context["dir"] / name
    holder = {}

    def runner():
        holder["result"] = slow_download(context["port"], release["id"], destination, pause=pause)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    wait_status(context["port"], active_downloads=1)
    return thread, holder, destination


def start_rehearsal(context, plan):
    holder = {}

    def runner():
        try:
            holder["result"] = api(context["port"], "POST",
                                  f"/api/plans/{plan['id']}/rehearse",
                                  {"revision": 2}, expected=200, timeout=40)
        except Exception as exc:
            holder["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if context["marker"].exists():
            break
        if "error" in holder:
            raise CheckFailure(f"rehearsal failed before taking demo lock: {holder['error']}")
        time.sleep(0.05)
    expect(context["marker"].exists(), "演示重演进入状态库写事务并留下锁标记")
    wait_status(context["port"], active_writes=1)
    return thread, holder


def start_seal(context, plan, revision=2):
    holder = {}

    def runner():
        try:
            holder["result"] = api(context["port"], "POST", f"/api/plans/{plan['id']}/seal",
                                    {"revision": revision}, expected=200, timeout=40)
        except Exception as exc:
            holder["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, holder


def assert_write_lock_held(state_path):
    db = sqlite3.connect(state_path, timeout=0)
    try:
        db.execute("PRAGMA busy_timeout=0")
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            expect("lock" in message or "busy" in message, f"观察到状态库写锁：{exc}")
            return
        raise CheckFailure("重演期间第二个写入者竟能取得 IMMEDIATE 锁")
    finally:
        db.close()


def assert_write_lock_released(state_path):
    db = sqlite3.connect(state_path, timeout=2)
    try:
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
    finally:
        db.close()
    ok("进程退出后 SQLite 写锁已释放，WAL 可恢复")


def verify_download(port, release, destination):
    status, data, _ = http_request(port, "GET", f"/api/plans/{release['id']}/download", timeout=20)
    expect(status == 200, f"重启后下载 {release['id']} 返回 200")
    destination.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    expect(digest == release["sha256"], f"完整下载 SHA-256 与凭证一致：{digest[:16]}…")
    expect(len(data) == release["size"], "下载字节数与凭证 candidate_size 一致")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        expect(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "下载候选通过 integrity_check")
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], "下载候选无外键违规")
        count = db.execute("SELECT COUNT(*) FROM bulk").fetchone()[0]
        expect(count == 60, f"下载候选可独立打开并读取 bulk（{count} 行）")
    finally:
        db.close()
    return digest


def verify_standard_download(port, plan_id, expected_choice_text=None):
    status, data, _ = http_request(port, "GET", f"/api/plans/{plan_id}/download", timeout=10)
    expect(status == 200, "恢复后的开放计划已封存并可下载")
    cert = api(port, "GET", f"/api/plans/{plan_id}/certificate")
    expect(hashlib.sha256(data).hexdigest() == cert["candidate_digest"], "续做后候选与凭证摘要一致")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        expect(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "续做候选完整性检查通过")
        if expected_choice_text is not None:
            row = db.execute("SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()
            expect(row is not None and row[0] == expected_choice_text, "续做候选包含 revision=2 的祖本选择")
    finally:
        db.close()


def direct_state_snapshot(state_path, plan_id):
    db = sqlite3.connect(state_path, timeout=2)
    try:
        row = db.execute("SELECT revision,state FROM plans WHERE id=?", (plan_id,)).fetchone()
        attempts = [r[0] for r in db.execute(
            "SELECT revision FROM attempts WHERE plan_id=? ORDER BY revision", (plan_id,))]
        return {"revision": row[0], "state": row[1], "attempt_revisions": attempts}
    finally:
        db.close()


def scenario_graceful(workdir):
    log("\n时序 A：先停止新操作/接入，等待已接受重演与大文件下载，再 SIGTERM 升级", CYAN)
    context = start_server(workdir, "A-graceful", slow=True)
    port = context["port"]
    try:
        plan = seed_open_plan_with_history(port)
        seal_plan = seed_open_plan_with_history(port)
        ready_to_seal = api(port, "POST", f"/api/plans/{seal_plan['id']}/rehearse",
                            {"revision": 2})
        expect(ready_to_seal["status"] == "passed", "第二个计划已准备好当前通过候选，用于演示在飞封存")
        release = seed_large_sealed_release(port)
        ok("已准备 revision=1 历史、待重演 revision=2、待封存计划和已封存大文件")

        download_thread, download_holder, download_path = start_download(
            context, release, "slow-download.bin", pause=0.12)
        context["gate"].write_text("3\n", encoding="utf-8")
        context["seal_gate"].write_text("1\n", encoding="utf-8")
        rehearsal_thread, rehearsal_holder = start_rehearsal(context, plan)
        assert_write_lock_held(context["state"])
        seal_thread, seal_holder = start_seal(context, seal_plan)
        wait_status(port, active_writes=2)

        os.kill(context["proc"].pid, signal.SIGUSR1)
        status = wait_status(port, mode="writes_draining")
        expect(status["accepting_writes"] is False, "SIGUSR1 后接入层停止接收新的写操作")

        rejected = api(port, "POST", f"/api/plans/{plan['id']}/decisions",
                       {"revision": 2, "item": plan["conflict"], "choice": "left"}, expected=503)
        expect(rejected["code"] == "draining", "排空期间新裁决立即收到 503 draining，而不是排队等写锁")

        history = api(port, "GET", f"/api/plans/{plan['id']}/attempts")
        expect([item["revision"] for item in history] == [1], "写锁重演期间仍可读取已提交历史 revision=1")
        plan_view = api(port, "GET", f"/api/plans/{plan['id']}")
        expect(plan_view["revision"] == 2 and plan_view["state"] == "open", "开放计划版本/状态可读取")
        ready = api(port, "GET", "/readyz", expected=503)
        expect(ready["mode"] == "writes_draining", "/readyz 返回 503，外部接入层可据此摘流")
        live = api(port, "GET", "/-/status")
        expect(live["active_writes"] == 2 and live["active_downloads"] == 1,
               f"停止接入前已确认重演、封存和大文件下载均在飞行中：{live}")

        log("    → 仅停止新接入/新写入，保持进程存活，等待已接受重演和封存……", YELLOW)
        wait_status(port, active_writes=0)
        rehearsal_thread.join(timeout=5)
        seal_thread.join(timeout=5)
        rehearsal = rehearsal_holder.get("result")
        expect(rehearsal and rehearsal.get("status") == "passed" and rehearsal.get("revision") == 2,
               "仅停接入流量后，已接受的 revision=2 重演持锁跑完并提交")
        seal_result = seal_holder.get("result")
        expect(seal_result and seal_result.get("revision") == 2 and "error" not in seal_holder,
               f"仅停接入流量后，已接受的封存排队获取写锁并原子提交：{seal_holder}")
        history = api(port, "GET", f"/api/plans/{plan['id']}/attempts")
        expect([item["revision"] for item in history] == [2, 1], "旧进程排空后即可读到完整历史 [2,1]")
        sealed_view = api(port, "GET", f"/api/plans/{seal_plan['id']}")
        expect(sealed_view["state"] == "sealed", "封存结果在旧进程内已落盘为 sealed")

        live = api(port, "GET", "/-/status")
        partial_size = download_path.stat().st_size if download_path.exists() else 0
        expect(download_thread.is_alive() and partial_size > 0,
               f"写入已排空，大文件客户端仍在慢速读取（已读 {partial_size} 字节）；服务端状态={live}")
        if live.get("active_downloads") != 1:
            warn("服务端写调用已提前进入本机内核发送缓冲；TCP/文件哈希仍会验证完整交付")
        log("    → 发送 SIGTERM 升级：关闭监听并让剩余下载/内核缓冲完成……", YELLOW)
        stop_server(context, timeout=25)
        download_thread.join(timeout=15)

        download = download_holder.get("result")
        expect(download and download["completed"], f"慢下载未被 SIGTERM 切断：{download}")
        expect(download["sha256"] == release["sha256"], "排空期间完成的大文件下载字节与凭证一致")
        expect(context["proc"].returncode == 0, f"服务优雅退出码为 0（实际 {context['proc'].returncode}）")

        restarted = start_server(workdir, "A-graceful")
        context.update(restarted)
        after = api(context["port"], "GET", f"/api/plans/{plan['id']}")
        expect(after["state"] == "open" and after["revision"] == 2, "重启后开放计划仍停留在 revision=2")
        history = api(context["port"], "GET", f"/api/plans/{plan['id']}/attempts")
        expect([item["revision"] for item in history] == [2, 1], "重启后重演历史完整可读：revision 2、1")
        sealed_certificate = api(context["port"], "GET",
                                 f"/api/plans/{seal_plan['id']}/certificate")
        expect(sealed_certificate["revision"] == 2, "在飞封存的凭证重启后可历史读取")

        release_view = api(context["port"], "GET", f"/api/plans/{release['id']}")
        expect(release_view["state"] == "archived", "重启后归档状态保留，下载/凭证等阅读资格不受影响")
        certificate = api(context["port"], "GET", f"/api/plans/{release['id']}/certificate")
        expect(certificate["candidate_digest"] == release["sha256"], "重启后封存凭证仍可历史读取")
        release_history = api(context["port"], "GET", f"/api/plans/{release['id']}/attempts")
        expect([item["revision"] for item in release_history] == [0], "归档发布计划的重演证据仍可读取")
        verify_download(context["port"], release, context["dir"] / "post-restart-download.sqlite")

        sealed = api(context["port"], "POST", f"/api/plans/{plan['id']}/seal", {"revision": 2})
        expect(sealed["revision"] == 2, "重启后可继续完成封存")
        verify_standard_download(context["port"], plan["id"], "祖本节点")
    finally:
        if context["proc"].poll() is None:
            stop_server(context)


def scenario_forced_kill(workdir):
    log("\n时序 B：重演持锁且大文件下载中直接 SIGKILL，再从 SQLite/WAL 恢复", CYAN)
    context = start_server(workdir, "B-forced", slow=True)
    port = context["port"]
    download_thread = rehearsal_thread = None
    try:
        plan = seed_open_plan_with_history(port)
        release = seed_large_sealed_release(port)
        ok("已准备与 A 相同的 revision 历史、开放计划和已封存大文件")

        download_thread, download_holder, download_path = start_download(
            context, release, "interrupted-download.bin", pause=0.04)
        context["gate"].write_text("30\n", encoding="utf-8")
        rehearsal_thread, rehearsal_holder = start_rehearsal(context, plan)
        assert_write_lock_held(context["state"])
        live = api(port, "GET", "/-/status")
        expect(live["active_writes"] == 1 and live["active_downloads"] == 1,
               f"发 SIGKILL 前已确认重演和下载均在飞行中：{live}")

        log("    → 直接 SIGKILL，不经过 drain/SIGTERM……", RED)
        context["proc"].kill()
        context["proc"].wait(timeout=10)
        expect(context["proc"].returncode == -signal.SIGKILL, "旧进程被 SIGKILL 终止")
        rehearsal_thread.join(timeout=5)
        download_thread.join(timeout=15)

        expect("result" not in rehearsal_holder and "error" in rehearsal_holder,
               "被强杀时重演调用没有伪造成功响应")
        download = download_holder.get("result")
        expect(download is not None and not download["completed"],
               f"强杀会中断当时正在传输的下载连接：{download}")
        if download and download["sha256"]:
            expect(download["sha256"] != release["sha256"], "中断下载的部分字节不能通过凭证 SHA-256 校验")
        else:
            ok("中断下载未形成任何可误认为完整候选的哈希")

        assert_write_lock_released(context["state"])
        snapshot = direct_state_snapshot(context["state"], plan["id"])
        expect(snapshot["revision"] == 2 and snapshot["state"] == "open",
               "强杀后计划仍是 revision=2/open，没有内存态丢失或状态改写")
        expect(snapshot["attempt_revisions"] == [1],
               f"未提交的 revision=2 重演随 SQLite 事务回滚，仅保留历史 {snapshot['attempt_revisions']}")

        context = start_server(workdir, "B-forced")
        history = api(context["port"], "GET", f"/api/plans/{plan['id']}/attempts")
        expect([item["revision"] for item in history] == [1], "恢复服务后历史读取只显示已提交 revision=1")

        retried = api(context["port"], "POST", f"/api/plans/{plan['id']}/rehearse", {"revision": 2})
        expect(retried["status"] == "passed", "重启后开放计划可重新重演 revision=2")
        repeated = api(context["port"], "POST", f"/api/plans/{plan['id']}/rehearse", {"revision": 2})
        expect(repeated == retried, "同版重演复用证据，重试幂等")
        api(context["port"], "POST", f"/api/plans/{plan['id']}/seal", {"revision": 2}, expected=200)
        ok("恢复后完成封存，开放计划无需人工改状态")
        verify_standard_download(context["port"], plan["id"], "祖本节点")

        release_view = api(context["port"], "GET", f"/api/plans/{release['id']}")
        expect(release_view["state"] == "archived", "强杀恢复后归档状态和阅读资格保留")
        certificate = api(context["port"], "GET", f"/api/plans/{release['id']}/certificate")
        expect(certificate["candidate_digest"] == release["sha256"], "强杀后旧封存凭证未损坏")
        verify_download(context["port"], release, context["dir"] / "post-recovery-download.sqlite")
    finally:
        if context["proc"].poll() is None:
            stop_server(context)


def write_summary(workdir):
    lines = [
        "# Pagefold 升级排空演示报告",
        "",
        "两个时序均已通过：",
        "",
        "1. **优雅排空**：`SIGUSR1`/`/readyz=503` 先停止新操作并供接入层摘流；进程不退出，已接受的持锁重演和排队等锁的封存继续到提交，旧进程内历史变为 `[2,1]`、封存计划变为 `sealed`。随后 `SIGTERM` 关闭监听并让仍在传输的大下载完整完成，退出码 0。重启后状态/历史/凭证保持，另一个开放计划可继续封存；排空期间下载与重启后下载均匹配凭证。",
        "2. **直接 SIGKILL**：未提交的 revision=2 重演事务回滚，写锁由 SQLite/WAL 恢复释放；旧历史 `[1]` 和已封存大文件保留。重启后重新重演、幂等复用、封存和下载校验均通过。被强杀中断的传输只产生不匹配凭证的部分文件；重新下载得到一致字节。",
        "",
        f"运行目录：`{workdir}`",
        "",
        "各时序目录包含 `server.log`、状态库和下载产物，可进一步检查。",
        "",
    ]
    (workdir / "demo-report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path,
                        default=ROOT / ".demo-runs" / time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    log(f"Pagefold deployment drain demo\n工作目录: {workdir}", GREEN)
    try:
        scenario_graceful(workdir)
        scenario_forced_kill(workdir)
    except Exception as exc:
        fail(str(exc))
        for server_log in workdir.glob("*/server.log"):
            print(f"\n--- tail {server_log} ---", file=sys.stderr)
            print(server_log.read_text(encoding="utf-8", errors="replace")[-6000:], file=sys.stderr)
        return 1
    write_summary(workdir)
    log("\n全部断言通过。", GREEN)
    print(f"报告：{workdir / 'demo-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
