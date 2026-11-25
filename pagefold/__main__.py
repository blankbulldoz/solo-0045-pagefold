import argparse
import sqlite3
from pathlib import Path

from .server import make_server
from .store import Store


def main():
    parser = argparse.ArgumentParser(
        description="以标准 WSGI 应用启动 Pagefold 数据页合流服务（stdlib WSGI 运行器）")
    parser.add_argument("--state", default="pagefold-state.sqlite", type=Path)
    parser.add_argument("--port", default=8745, type=int)
    parser.add_argument("--allowed-host", action="append", default=None,
                        help="额外允许的 Host（形如 主机:端口），可重复；默认仅允许本机回环地址")
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37) or not all(hasattr(sqlite3.Connection, name)
                                                          for name in ("deserialize", "serialize")):
        parser.exit(2, "需要带 deserialize 的 SQLite 3.37+。\n")
    with make_server(Store(args.state), args.port, args.allowed_host) as server:
        print(f"Pagefold WSGI 已启动，访问 http://127.0.0.1:{server.server_port}", flush=True)
        print("生产环境可改用：gunicorn 'pagefold.wsgi:application'（PAGEFOLD_STATE 指定状态库）",
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
