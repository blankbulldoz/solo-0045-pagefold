#!/usr/bin/env python3
"""构建供前端与数据组对照的可登记 SQLite 镜像。

数据母版来自仓库 pagefold/demo.py 的 node 示例（石英/蔚蓝/赭石三方镜像里的
bench / node / binding 三张表）。在保留母版原始行与原始建表语句的前提下，向 node
表追加三类探针材料：

  1. 超出 JavaScript 安全整数范围（Number.MAX_SAFE_INTEGER = 2^53-1）的复合键；
  2. 空 BLOB（x''）与 NULL 的并列样本；
  3. 显示相同但 SQLite 存储类型不同（typeof 不同）的业务值。

另建一张 STRICT ... ANY 的无亲和探针表 type_probe，专门演示整数/实数/文本/BLOB
在不发生任何亲和转换时的存储类型与 SQLite 比较归类。

脚本只写数据库文件，读取逻辑与登记校验全部走仓库内的 pagefold 包，不复制其实现。
以 `python3 -I` 运行，路径均通过命令行参数传入。
"""

import argparse
import sqlite3
from pathlib import Path

# 母版建表语句：与 pagefold/demo.py SCHEMA 逐字一致（node_weight 索引保留）。
MASTER_SCHEMA = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    note TEXT, payload BLOB, weight REAL CHECK(weight >= 0),
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""

# 无亲和探针表：STRICT + ANY 列禁止一切存储转换，WITHOUT ROWID 且显式 BINARY 主键，
# 满足 Pagefold 对可登记镜像的表形要求（显式、非空、BINARY 比较主键）。
PROBE_SCHEMA = """
CREATE TABLE type_probe(
    code TEXT PRIMARY KEY,
    sample ANY
) WITHOUT ROWID, STRICT;
"""

BENCH_ROWS = [
    # 母版原始两行（pagefold/demo.py）
    ("north", "玄石"),
    ("south", "砂层"),
    # 探针行使用的父键
    ("east", "东线探针"),
]

# node 行：列序 (bench, slot, note, payload, weight)。
# 前两行与 demo.py 石英镜像逐值一致，作为母版锚点；其余为追加探针。
NODE_ROWS = [
    # --- 母版锚点（注意 south/1 携带空 BLOB b""，与 NULL 截然不同）---
    ("north", 1, "祖本节点", b"\x00\x14", 1.5),
    ("south", 1, "稳定节点", b"", 3.0),
    # --- 超出 JavaScript 安全整数范围的复合键（slot > 2^53-1）---
    # 9007199254740993 == 2^53+1：JS Number 会舍入成 9007199254740992
    ("east", 9007199254740993, "超出安全整数的复合键", b"\x90\x07", 5.5),
    # 9223372036854775807 == 2^63-1：SQLite 64 位有符号整数上限，JS 完全无法精确保留
    ("east", 9223372036854775807, "SQLite 整数上限键", None, 6.25),
    # 负向超大整数：JS 同样丢精度
    ("east", -9223372036854775808, "负向整数下限键", b"", 7.0),
    # --- 空 BLOB 与 NULL 并列，键不同、可分别定位 ---
    ("east", 100, "空 BLOB 载体", b"", 0.0),
    ("east", 101, "NULL 载体", None, 0.0),
    # --- 显示相同、存储类型不同的业务值（payload 为 BLOB 亲和，原值不被转换）---
    # 整数 3 与实数 3.0：SQLite 中 3 = 3.0 成立，但 typeof 分别为 integer / real
    ("east", 200, "整数业务值", 3, 1.0),
    ("east", 201, "实数业务值", 3.0, 1.0),
    # 文本 '3' 与 BLOB x'33'：渲染都像 "3"，但分别是 text / blob，比较互不相等
    ("east", 202, "文本业务值", "3", 1.0),
    ("east", 203, "二进制业务值", b"3", 1.0),
    # --- 空串文本与空 BLOB 对照（note 为 TEXT 亲和，payload 为 BLOB 亲和）---
    ("east", 300, "", b"", 2.0),
    # --- 主键亲和归一演示：写入实数 4.0，INTEGER 亲和的 slot 实际存成整数 4 ---
    ("east", 4.0, "实数写入整数亲和键", None, 2.5),
]

# binding 母版锚点 + 指向超大复合键的外键行，证明大整数键可被外键精确引用。
BINDING_ROWS = [
    (1, "north", 1),
    (2, "east", 9007199254740993),
]

# type_probe：同一"显示值"在无亲和列里的四种存储类型，外加空值与 NULL 样本。
# 每行都有独立文本主键 code，任何一个值都能用 code 精确定位。
PROBE_ROWS = [
    ("num-int", 3),       # typeof = integer
    ("num-real", 3.0),    # typeof = real，   与 num-int 显示相同、SQLite 等值
    ("num-text", "3"),    # typeof = text
    ("num-blob", b"3"),   # typeof = blob，字节 0x33
    ("empty-text", ""),   # 空文本
    ("empty-blob", b""),  # 空 BLOB，与空文本显示都为空但类型不同
    ("null-sample", None),
]


def build(target: Path) -> None:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(MASTER_SCHEMA)
        db.executescript(PROBE_SCHEMA)
        db.executemany("INSERT INTO bench VALUES(?,?)", BENCH_ROWS)
        db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", NODE_ROWS)
        db.executemany("INSERT INTO binding VALUES(?,?,?)", BINDING_ROWS)
        db.executemany("INSERT INTO type_probe VALUES(?,?)", PROBE_ROWS)
        db.commit()

        # 构建期自检：外键与完整性必须通过，否则不产出文件。
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check(1)").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM node").fetchone()[0] == len(NODE_ROWS)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(db.serialize())
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 node 示例对照用 SQLite 镜像")
    parser.add_argument("output", type=Path, help="输出 .sqlite 文件路径")
    args = parser.parse_args()
    build(args.output)
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
