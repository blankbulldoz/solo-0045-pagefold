#!/usr/bin/env python3
"""Rootless local supervisor/upgrade helper for Pagefold.

Examples:
    python3 deploy/pagefold_ctl.py start
    python3 deploy/pagefold_ctl.py drain --wait-writes
    python3 deploy/pagefold_ctl.py upgrade
    python3 deploy/pagefold_ctl.py status
    python3 deploy/pagefold_ctl.py stop

The controller stores only a PID file and log under --runtime (default:
./.runtime).  SQLite state lives at <runtime>/pagefold-state.sqlite.
For boot-time operation, use deploy/systemd/pagefold.service instead.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = Path(os.environ.get("PAGEFOLD_RUNTIME", ROOT / ".runtime"))


def pid_file(runtime):
    return runtime / "pagefold.pid"


def log_file(runtime):
    return runtime / "pagefold.log"


def state_file(runtime):
    return runtime / "pagefold-state.sqlite"


def read_pid(runtime):
    path = pid_file(runtime)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="ascii").strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def wait_health(port, proc, log_path, timeout=25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.exists() else ""
            raise SystemExit(f"Pagefold exited with {proc.returncode}\n{tail}")
        try:
            with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                if response.status == 200:
                    return json.loads(response.read())
        except Exception as exc:
            last = exc
            time.sleep(0.15)
    raise SystemExit(f"Pagefold did not become healthy: {last}")


def fetch_status(port):
    with urlopen(f"http://127.0.0.1:{port}/-/status", timeout=3) as response:
        return json.loads(response.read())


def cmd_start(args):
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    pid = read_pid(runtime)
    if pid:
        print(f"Pagefold is already running (pid {pid})")
        return 0
    log = log_file(runtime).open("ab", buffering=0)
    cmd = [
        sys.executable, "-m", "pagefold",
        "--state", str(state_file(runtime)),
        "--port", str(args.port),
        "--drain-timeout", str(args.drain_timeout),
        "--sqlite-timeout", str(args.sqlite_timeout),
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    pid_file(runtime).write_text(str(proc.pid), encoding="ascii")
    status = wait_health(args.port, proc, log_file(runtime))
    print(f"Pagefold started: pid={proc.pid} url=http://127.0.0.1:{args.port} mode={status['mode']}")
    print(f"state={state_file(runtime)} log={log_file(runtime)}")
    return 0


def cmd_status(args):
    pid = read_pid(args.runtime.resolve())
    if not pid:
        print("stopped")
        return 1
    try:
        status = fetch_status(args.port)
    except Exception as exc:
        print(f"pid={pid} alive but HTTP status failed: {exc}")
        return 2
    print(json.dumps({"pid": pid, "url": f"http://127.0.0.1:{args.port}", **status},
                     ensure_ascii=False, indent=2))
    return 0


def send_signal(runtime, signum):
    pid = read_pid(runtime)
    if not pid:
        raise SystemExit("Pagefold is not running")
    os.kill(pid, signum)
    return pid


def cmd_drain(args):
    runtime = args.runtime.resolve()
    pid = send_signal(runtime, signal.SIGUSR1)
    print(f"Sent SIGUSR1 to pid {pid}; new writes now receive 503 draining")
    if args.wait_writes:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            status = fetch_status(args.port)
            if status["active_writes"] == 0:
                print(f"Accepted writes drained: {json.dumps(status, ensure_ascii=False)}")
                return 0
            time.sleep(0.2)
        raise SystemExit("Timed out waiting for active_writes=0")
    return 0


def cmd_stop(args):
    runtime = args.runtime.resolve()
    pid = read_pid(runtime)
    if not pid:
        print("Pagefold is not running")
        return 0
    print(f"Stopping pid {pid} with SIGTERM (drain timeout {args.timeout}s)...")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            pid_file(runtime).unlink(missing_ok=True)
            print("Pagefold stopped cleanly")
            return 0
        time.sleep(0.2)
    print(f"Graceful stop timed out; sending SIGKILL to process group {pid}", file=sys.stderr)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        os.kill(pid, signal.SIGKILL)
    pid_file(runtime).unlink(missing_ok=True)
    return 124


def cmd_upgrade(args):
    """Stop writes, let accepted writes finish, then graceful restart.

    Replace files / swap the release symlink before invoking this command.
    SQLite state and sealed candidates stay under --runtime and are reused.
    """
    runtime = args.runtime.resolve()
    if read_pid(runtime):
        cmd_drain(argparse.Namespace(runtime=runtime, port=args.port, timeout=args.write_timeout,
                                    wait_writes=True))
        cmd_stop(argparse.Namespace(runtime=runtime, port=args.port, timeout=args.stop_timeout))
    return cmd_start(argparse.Namespace(runtime=runtime, port=args.port,
                                        drain_timeout=args.drain_timeout,
                                        sqlite_timeout=args.sqlite_timeout))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PAGEFOLD_PORT", "8745")))
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--drain-timeout", type=float, default=45.0)
    start.add_argument("--sqlite-timeout", type=float, default=30.0)
    start.set_defaults(func=cmd_start)

    sub.add_parser("status").set_defaults(func=cmd_status)

    drain = sub.add_parser("drain")
    drain.add_argument("--wait-writes", action="store_true")
    drain.add_argument("--timeout", type=float, default=60.0)
    drain.set_defaults(func=cmd_drain)

    stop = sub.add_parser("stop")
    stop.add_argument("--timeout", type=float, default=60.0)
    stop.set_defaults(func=cmd_stop)

    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--write-timeout", type=float, default=60.0)
    upgrade.add_argument("--stop-timeout", type=float, default=90.0)
    upgrade.add_argument("--drain-timeout", type=float, default=75.0)
    upgrade.add_argument("--sqlite-timeout", type=float, default=30.0)
    upgrade.set_defaults(func=cmd_upgrade)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
