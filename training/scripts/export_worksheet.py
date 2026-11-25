"""从三份练习镜像导出行值对照，生成练习者第一轮的纸面工作底稿数据。

只读取镜像内容并输出 Markdown/JSON，不依赖当前工作目录，以 -I 模式运行。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROLES = [("base 祖本", "base-ledger.sqlite"),
         ("left 左支", "left-branch.sqlite"),
         ("right 右支", "right-branch.sqlite")]
TABLES = ["ledger", "citation"]


def dump(path, table):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(path.read_bytes())
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        rows = db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        return cols, rows
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="导出练习者行值对照底稿")
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    lines = ["# 第一轮纸面底稿：三支行值对照（由镜像直接导出）", ""]
    for table in TABLES:
        lines.append(f"## 表 {table}")
        lines.append("")
        for label, filename in ROLES:
            cols, rows = dump(args.images / filename, table)
            lines.append(f"### {label}（{filename}）")
            lines.append("")
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("| " + " | ".join("---" for _ in cols) + " |")
            for row in rows:
                lines.append("| " + " | ".join("（缺席/已删除）" if v is None else str(v) for v in row) + " |")
            lines.append("")
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
