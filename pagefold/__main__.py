import argparse
import sqlite3
from pathlib import Path

from .server import make_server
from .sources import SourceConfigError, SourcePolicy
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="启动 Pagefold 数据页合流服务")
    parser.add_argument("--state", default="pagefold-state.sqlite", type=Path)
    parser.add_argument("--port", default=8745, type=int)
    parser.add_argument("--sources-config", "--source-config", dest="sources_config",
                        type=Path, help="允许采集的本机 SQLite 源 JSON 配置")
    parser.add_argument("--allow-source", "--source", action="append", default=[],
                        metavar="NAME=PATH", dest="allow_sources",
                        help="追加一个允许采集的源，可重复")
    parser.add_argument("--source-root", action="append", default=[], type=Path,
                        help="追加一个允许按文件名采集的目录，可重复")
    args = parser.parse_args()
    if sqlite3.sqlite_version_info < (3, 37) or not all(hasattr(sqlite3.Connection, name)
                                                          for name in ("deserialize", "serialize")):
        parser.exit(2, "需要带 deserialize 的 SQLite 3.37+。\n")
    try:
        sources = SourcePolicy.from_config(
            args.sources_config, args.allow_sources, args.source_root)
    except SourceConfigError as exc:
        parser.exit(2, f"采集源配置无效：{exc}\n")
    with make_server(Store(args.state), args.port, sources) as server:
        print(f"Pagefold 已启动，访问 http://127.0.0.1:{server.server_port}", flush=True)
        if sources.enabled:
            print(f"已允许 {len(sources.sources)} 个命名采集源、{len(sources.roots)} 个源目录", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
