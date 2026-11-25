#!/usr/bin/env python3
"""读法二：直接用 SQLite 查询读取镜像，并显式标注存储类型与比较归类。

与读法一（Pagefold JSON）对照。本脚本只使用 sqlite3 标准库，模拟前端/数据组直接连库
时看到的世界：值按 SQLite 原生动态类型返回，另用 typeof() 标注真实存储类型，并用
查询演示哪些值在 SQLite 比较语义下会被归为一起。产出：

  sqlite-view.jsonl   每行一条，含复合键、原始值、typeof 与 JS 安全整数告警
  sqlite-equality.md  等值/归类查询的实际结果（整数=实数、文本/BLOB 各自独立、NULL）

以 `python3 -I` 运行。
"""

import argparse
import json
from pathlib import Path

JS_MAX_SAFE = 2**53 - 1  # Number.MAX_SAFE_INTEGER
JS_MIN_SAFE = -(2**53 - 1)


def display(value):
    """模拟浏览器/通用 SQL 客户端把单元格渲染成字符串后的样子（类型在此被抹平）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return value.decode("latin-1")
    if isinstance(value, float) and value.is_integer():
        # 3.0 在多数客户端显示为 "3"，与整数 3 显示一致——这正是陷阱所在
        return str(int(value))
    return str(value)


def jsonable(value):
    """让 sqlite-view.jsonl 不丢类型：BLOB 显式标注 base64 与原始字节，None 保留 null。"""
    import base64

    if isinstance(value, bytes):
        return {"__sqlite_type": "blob", "base64": base64.b64encode(value).decode("ascii"),
                "byte_length": len(value)}
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite 直连读法与比较归类分析")
    parser.add_argument("mirror", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    import sqlite3

    db = sqlite3.connect(args.mirror)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")

    records = []

    # ---- node：复合键 (bench, slot)，逐值标注 typeof 与 JS 精度风险 ----
    for row in db.execute(
        "SELECT bench, slot, note, payload, weight, "
        "typeof(bench) t_bench, typeof(slot) t_slot, typeof(note) t_note, "
        "typeof(payload) t_payload, typeof(weight) t_weight FROM node ORDER BY bench, slot"
    ):
        slot = row["slot"]
        js_risk = (
            isinstance(slot, int) and not (JS_MIN_SAFE <= slot <= JS_MAX_SAFE)
        )
        records.append(
            {
                "table": "node",
                "key_columns": ["bench", "slot"],
                "key": [row["bench"], slot],
                "key_storage_types": [row["t_bench"], row["t_slot"]],
                "cells": [
                    {"column": "note", "value": jsonable(row["note"]), "typeof": row["t_note"],
                     "display": display(row["note"])},
                    {"column": "payload", "value": jsonable(row["payload"]), "typeof": row["t_payload"],
                     "display": display(row["payload"])},
                    {"column": "weight", "value": jsonable(row["weight"]), "typeof": row["t_weight"],
                     "display": display(row["weight"])},
                ],
                "js_safe_integer_warning": (
                    f"slot={slot} 超出 JS 安全整数范围，JSON.parse/Number 会丢精度"
                    if js_risk
                    else None
                ),
            }
        )

    # ---- type_probe：无亲和列，存储类型最干净 ----
    for row in db.execute(
        "SELECT code, sample, typeof(sample) t_sample FROM type_probe ORDER BY code"
    ):
        records.append(
            {
                "table": "type_probe",
                "key_columns": ["code"],
                "key": [row["code"]],
                "key_storage_types": ["text"],
                "cells": [
                    {"column": "sample", "value": jsonable(row["sample"]), "typeof": row["t_sample"],
                     "display": display(row["sample"])}
                ],
                "js_safe_integer_warning": None,
            }
        )

    jsonl_path = args.out_dir / "sqlite-view.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    # ---- 比较归类：用真实查询回答“哪些值在 SQLite 看来是一起的” ----
    def scalar(sql, params=()):
        return db.execute(sql, params).fetchone()[0]

    eq_int_real = scalar("SELECT 3 = 3.0")
    type_int = scalar("SELECT typeof(3)")
    type_real = scalar("SELECT typeof(3.0)")
    eq_text_blob = scalar("SELECT '3' = x'33'")
    eq_empty = scalar("SELECT '' = x''")
    eq_text_int = scalar("SELECT '3' = 3")
    null_eq = scalar("SELECT NULL = NULL")
    null_is = scalar("SELECT NULL IS NULL")
    null_by_eq = scalar("SELECT count(*) FROM type_probe WHERE sample = NULL")
    null_by_is = scalar("SELECT count(*) FROM type_probe WHERE sample IS NULL")

    equality_lines = [
        "# SQLite 比较归类实测（node-reference.sqlite）",
        "",
        "下列结果全部由对交付库的真实查询产生。注意 SQLite 动态类型与亲和规则：",
        "",
        "## 1. 整数与精确等值的实数在比较时归为同一值",
        "",
        "```",
        f"SELECT 3 = 3.0;                                  -> {eq_int_real}",
        f"typeof(3)={type_int}  typeof(3.0)={type_real}",
        "```",
        "",
        "type_probe 中 num-int(sample=3, integer) 与 num-real(sample=3.0, real)：",
        "",
        "```",
    ]
    for r in db.execute(
        "SELECT a.code, b.code, a.sample = b.sample AS eq, typeof(a.sample), typeof(b.sample) "
        "FROM type_probe a JOIN type_probe b ON a.sample = b.sample "
        "WHERE a.code IN ('num-int','num-real') ORDER BY a.code, b.code"
    ):
        equality_lines.append(f"  {r[0]:9s} = {r[1]:9s} -> {r[2]}  ({r[3]} vs {r[4]})")
    equality_lines += [
        "```",
        "",
        "GROUP BY 同样把两者归并（一个比较类，两行）：",
        "",
        "```",
    ]
    for r in db.execute(
        "SELECT sample, typeof(sample), count(*) FROM type_probe "
        "WHERE code IN ('num-int','num-real') GROUP BY sample"
    ):
        equality_lines.append(f"  值={r[0]!r:6} typeof={r[1]:8} 组内行数={r[2]}")
    equality_lines += [
        "```",
        "",
        "## 2. 文本与 BLOB 各自独立，即便字节/显示相同也不相等",
        "",
        "```",
        f"SELECT '3' = x'33';        -> {eq_text_blob}   -- text vs blob",
        f"SELECT '' = x'';           -> {eq_empty}   -- 空文本 vs 空 BLOB",
        f"SELECT '3' = 3;            -> {eq_text_int}   -- text vs integer",
        "```",
        "",
        "type_probe 四样本的等值连通情况（仅自身与自身/数值同类相等）：",
        "",
        "```",
    ]
    for r in db.execute(
        "SELECT a.code, b.code, a.sample = b.sample AS eq "
        "FROM type_probe a JOIN type_probe b "
        "ON a.code IN ('num-int','num-real','num-text','num-blob') "
        "AND b.code IN ('num-int','num-real','num-text','num-blob') "
        "WHERE a.sample = b.sample OR a.code = b.code ORDER BY a.code, b.code"
    ):
        equality_lines.append(f"  {r[0]:9s} ~ {r[1]:9s} -> {r[2]}")
    equality_lines += [
        "```",
        "",
        "## 3. NULL 不参与等值，只能用 IS 定位",
        "",
        "```",
        f"SELECT NULL = NULL;        -> {('NULL' if null_eq is None else null_eq)}  (NULL，非真)",
        f"SELECT NULL IS NULL;       -> {null_is}",
        "type_probe 中 NULL 行数:",
        f"  WHERE sample = NULL  -> {null_by_eq} 行（= 比较找不到 NULL）",
        f"  WHERE sample IS NULL -> {null_by_is} 行（定位 NULL 的正确写法）",
        "```",
        "",
        "## 4. node 表 payload 同显示值的比较归类",
        "",
        "```",
    ]
    for r in db.execute(
        "SELECT slot, payload, typeof(payload), payload = 3 AS eq_int3 "
        "FROM node WHERE bench='east' AND slot IN (200,201,202,203) ORDER BY slot"
    ):
        shown = r[1].decode("latin-1") if isinstance(r[1], bytes) else r[1]
        equality_lines.append(
            f"  slot={r[0]:3} payload={shown!r:6} typeof={r[2]:8} payload=3 -> {r[3]}"
        )
    equality_lines += [
        "```",
        "",
        "## 5. 空 BLOB 与 NULL 在 node 中的定位差异",
        "",
        "```",
    ]
    for r in db.execute(
        "SELECT slot, typeof(payload) FROM node WHERE bench='east' "
        "AND (payload IS NULL OR payload = x'') ORDER BY slot"
    ):
        equality_lines.append(f"  slot={r[0]}: typeof(payload)={r[1]}")
    equality_lines += [
        "```",
        "",
        "## 6. 整数亲和主键的归一：写 4.0 实际存为 integer 4",
        "",
        "```",
    ]
    for r in db.execute("SELECT slot, typeof(slot) FROM node WHERE bench='east' AND slot=4"):
        equality_lines.append(f"  写入 4.0 后 slot={r[0]} typeof={r[1]}（再插 4.0 会撞唯一约束）")
    equality_lines.append("```")

    md_path = args.out_dir / "sqlite-equality.md"
    md_path.write_text("\n".join(equality_lines) + "\n", encoding="utf-8")

    db.close()
    print(f"wrote {jsonl_path} ({len(records)} rows)")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
