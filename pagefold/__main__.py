import argparse
import sqlite3
from pathlib import Path

from .errors import Problem
from .keyring import Keyring
from .server import make_server
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="启动 Pagefold 数据页合流服务")
    parser.add_argument("--state", default="pagefold-state.sqlite", type=Path)
    parser.add_argument("--port", default=8745, type=int)
    parser.add_argument("--keys", type=Path, default=None,
                        help="组织签名密钥目录；配置后可颁发交接凭证")
    parser.add_argument("--init-keys", action="store_true",
                        help="在 --keys 目录初始化新的演练密钥环后启动")
    parser.add_argument("--organization", default="pagefold-drill-organization",
                        help="初始化密钥环时写入的组织名称")
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37) or not all(hasattr(sqlite3.Connection, name)
                                                          for name in ("deserialize", "serialize")):
        parser.exit(2, "需要带 deserialize 的 SQLite 3.37+。\n")
    keyring = None
    if args.keys is not None:
        keyring = Keyring(args.keys)
        if args.init_keys:
            try:
                keyring.initialize(args.organization)
            except Problem as exc:
                parser.exit(2, f"{exc.detail}\n")
            print(f"已初始化组织密钥环：{args.keys}", flush=True)
        elif not keyring.exists():
            parser.exit(2, f"密钥目录 {args.keys} 不存在；可加 --init-keys 初始化演练密钥环。\n")
    with make_server(Store(args.state), args.port, keyring) as server:
        print(f"Pagefold 已启动，访问 http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
