import argparse
import sqlite3
from pathlib import Path

from .errors import Problem
from .server import make_server
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="启动 Pagefold 数据页合流服务")
    parser.add_argument("--state", default="pagefold-state.sqlite", type=Path)
    parser.add_argument("--port", default=8745, type=int)
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37) or not all(hasattr(sqlite3.Connection, name)
                                                          for name in ("deserialize", "serialize")):
        parser.exit(2, "需要带 deserialize 的 SQLite 3.37+。\n")
    try:
        store = Store(args.state)
    except Problem as exc:
        parser.exit(2, f"{exc.detail}\n")
    with make_server(store, args.port) as server:
        print(f"Pagefold 已启动，访问 http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
