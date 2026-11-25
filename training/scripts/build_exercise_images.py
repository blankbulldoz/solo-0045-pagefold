"""裁决练习：生成同一批三份 SQLite 镜像（祖本 / 左支 / 右支）。

三份镜像模式精确一致，只在数据行上分叉，覆盖三种教学情形：
  1. 双方相同插入（L-101，左右两支独立做出同值插入 -> 自动采纳）；
  2. 删改分叉（L-102，左支删除、右支改写 -> 唯一人工冲突项）；
  3. 来自不同分支的引用行为（C-210 左支引用、C-300 右支新增引用 -> 自动采纳）。

镜像以检查点后的独立字节序列写出（journal_mode=DELETE 并关闭连接），
可直接通过 Pagefold 的 /api/images 入口登记。
"""
import argparse
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE ledger(
    ledger_id TEXT PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    site_note TEXT,
    auto_flag TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE TABLE citation(
    ref_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL REFERENCES ledger(ledger_id)
) WITHOUT ROWID;
CREATE INDEX citation_ledger_idx ON citation(ledger_id);
"""

# 祖本（base）：裁决的共同祖先。
# L-102 的标题“旧版标题”是被拒组合的伏笔：若冲突时保留祖本 L-102，
# 它会与两支同值插入、被系统自动采纳的 L-101 在 title 唯一约束上相撞。
BASE_LEDGER = [
    ("L-100", "共享总账", "祖本现场备注", "基准备注"),
    ("L-102", "旧版标题", "待处理现场备注", "旧标记"),
]
BASE_CITATION = [
    ("C-200", "L-100"),
]

# 左支（left）：插入与右支同值的 L-101（沿用旧标题）、删除 L-102，
# 并补一条指向 L-100 的引用。
LEFT_LEDGER = [
    ("L-100", "共享总账", "祖本现场备注", "基准备注"),
    ("L-101", "旧版标题", "共建现场备注", "共建标记"),
    # L-102 被左支删除
]
LEFT_CITATION = [
    ("C-200", "L-100"),
    ("C-210", "L-100"),
]

# 右支（right）：插入与左支同值的 L-101、把 L-102 改写到新标题，
# 并新增另一条指向 L-100 的引用。两支引用行为来自不同分支。
RIGHT_LEDGER = [
    ("L-100", "共享总账", "祖本现场备注", "基准备注"),
    ("L-101", "旧版标题", "共建现场备注", "共建标记"),
    ("L-102", "双方共建账", "右支改写备注", "右支标记"),
]
RIGHT_CITATION = [
    ("C-200", "L-100"),
    ("C-300", "L-100"),
]


def build(flavor, ledger_rows, citation_rows):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO ledger(ledger_id,title,site_note,auto_flag) VALUES(?,?,?,?)", ledger_rows)
        db.executemany("INSERT INTO citation(ref_id,ledger_id) VALUES(?,?)", citation_rows)
        db.commit()
        data = db.serialize()
    finally:
        db.close()
    # 自检：每份镜像自身都必须外键完整、可独立打开。
    check = sqlite3.connect(":memory:")
    try:
        check.deserialize(data)
        assert check.execute("PRAGMA foreign_key_check").fetchall() == []
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    return data


def main():
    parser = argparse.ArgumentParser(description="生成裁决练习的三份 Pagefold 镜像")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    flavors = {
        "base-ledger.sqlite": ("base", BASE_LEDGER, BASE_CITATION),
        "left-branch.sqlite": ("left", LEFT_LEDGER, LEFT_CITATION),
        "right-branch.sqlite": ("right", RIGHT_LEDGER, RIGHT_CITATION),
    }
    for filename, (flavor, ledger, citation) in flavors.items():
        target = args.directory / filename
        if target.exists():
            raise SystemExit(f"目标已存在，避免覆盖：{target}")
        target.write_bytes(build(flavor, ledger, citation))
        print(target)


if __name__ == "__main__":
    main()
