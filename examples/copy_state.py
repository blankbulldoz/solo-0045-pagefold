"""制作状态库的一致只读副本，供交接核对使用。

Pagefold 状态库使用 WAL。服务运行期间直接复制 state.sqlite 可能漏掉尚在
state.sqlite-wal 中的提交，因此请用本脚本（SQLite 备份 API）或
`sqlite3 state.sqlite ".backup copy.sqlite"` 制作副本。服务可以保持运行。

用法：python3 -m examples.copy_state pagefold-state.sqlite handover/state.sqlite
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="制作 Pagefold 状态库的一致副本")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.target.exists():
        parser.exit(2, f"目标 {args.target} 已存在，请先移除，避免覆盖旧证据。\n")
    if not args.source.is_file():
        parser.exit(2, f"找不到源状态库 {args.source}。\n")
    args.target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{args.source.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(args.target)
    try:
        with target:
            source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            parser.exit(1, f"副本完整性检查失败：{check}\n")
    finally:
        target.close()
        source.close()
    print(f"已写出 {args.target}（integrity_check=ok）")


if __name__ == "__main__":
    sys.exit(main())
