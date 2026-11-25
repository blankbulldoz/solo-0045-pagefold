import argparse
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

from .server import make_server
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动 Pagefold 数据页合流服务")
    parser.add_argument("--state", default="pagefold-state.sqlite", type=Path)
    parser.add_argument("--port", default=8745, type=int)
    parser.add_argument("--drain-timeout", type=float,
                        default=float(os.environ.get("PAGEFOLD_DRAIN_TIMEOUT", "45")))
    parser.add_argument("--sqlite-timeout", type=float,
                        default=float(os.environ.get("PAGEFOLD_SQLITE_TIMEOUT", "30")))
    args = parser.parse_args(argv)
    if sqlite3.sqlite_version_info < (3, 37) or not all(hasattr(sqlite3.Connection, name)
                                                          for name in ("deserialize", "serialize")):
        parser.exit(2, "需要带 deserialize 的 SQLite 3.37+。\n")

    server = make_server(Store(args.state, args.sqlite_timeout), args.port, args.drain_timeout)
    stop_event = threading.Event()

    def request_stop(_signum=None, _frame=None):
        # Signal handlers run in the main thread. The watcher performs the
        # blocking shutdown() call from another thread as required by
        # socketserver.
        stop_event.set()

    def stop_accepting():
        stop_event.wait()
        print("Pagefold 收到停止信号：关闭新连接入口并等待已接受请求", flush=True)
        server.lifecycle.begin_shutdown()
        try:
            server.shutdown()
        except Exception:
            pass

    def drain_writes(_signum, _frame):
        server.lifecycle.drain_writes()
        print("Pagefold 已停止接收新写入；已接受的重演、封存和下载继续", flush=True)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, drain_writes)

    threading.Thread(target=stop_accepting, name="pagefold-stop", daemon=True).start()
    print(f"Pagefold 已启动，访问 http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        # Close the listening socket before waiting on handler threads. This
        # stops new ingress immediately while admitted writes/downloads drain.
        server.server_close()
        graceful = True
        if stop_event.is_set():
            graceful = server.lifecycle.wait_until_idle(args.drain_timeout)
            if not graceful:
                print(f"Pagefold 排空超时：{server.lifecycle.status()}", file=sys.stderr, flush=True)
        if stop_event.is_set() and graceful:
            print("Pagefold 已完成排空并停止", flush=True)
        elif stop_event.is_set():
            # Handler threads are daemons; returning lets the process exit.
            os._exit(124)


if __name__ == "__main__":
    main()
