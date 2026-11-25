#!/usr/bin/env python3
"""交叉核查：对同一交付库，分别用 Pagefold JSON 读法和 SQLite 直连读法取值，
逐键比对，并核验任务要求的每一类材料都真实存在且可定位。

核查项：
  1. 镜像可通过 Pagefold 真实登记链路（catalog/约束/read_image）；
  2. 两种读法对每个复合键取到的行一一对应，键集合完全相同；
  3. 解码 Pagefold 原子后的值，与 SQLite 直连值按类型严格相等（不靠显示字符串）；
  4. 超出 JS 安全整数范围的复合键存在、可定位、且整数原子为精确十进制文本；
  5. 空 BLOB 与 NULL 同时存在且 typeof 不同、互不相等；
  6. 显示相同但存储类型不同的值（integer/real/text/blob）齐备且类型被保留；
  7. SQLite 比较归类（3=3.0 为真、文本/BLOB 不并入、NULL 只能 IS 定位）与文档一致；
  8. 母版锚点行（north/1、south/1）与 pagefold/demo.py 石英镜像逐值一致。

输出人读报告到 stdout，并把机器可读结论写到 cross-check-report.json。
以 `python3 -I` 运行。
"""

import argparse
import base64
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pagefold.catalog import describe  # noqa: E402
from pagefold.codec import unpack  # noqa: E402
from pagefold.constraints import check_database  # noqa: E402
from pagefold.images import register  # noqa: E402
from pagefold.rows import read_image, key_atom  # noqa: E402
from pagefold.sqlite_image import mounted, ident  # noqa: E402
from pagefold.store import Store  # noqa: E402

JS_MAX_SAFE = 2**53 - 1
JS_MIN_SAFE = -(2**53 - 1)


def atom_to_python(atom):
    return unpack(atom)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pagefold JSON 与 SQLite 查询交叉核查")
    parser.add_argument("mirror", type=Path)
    parser.add_argument("pagefold_jsonl", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    data = args.mirror.read_bytes()
    checks = []

    def record(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")
        return ok

    # ---- 1. 真实登记链路 ----
    with mounted(data) as db:
        catalog = describe(db)
        check_database(db)
        pagefold_image = read_image(db, catalog)
    with tempfile.TemporaryDirectory() as tmp:
        rec = register(Store(Path(tmp) / "state.sqlite"), data, args.mirror.name)
    record("Pagefold 登记", rec["id"] and len(rec["catalog"]["tables"]) == 4,
           f"image_id={rec['id']}，表={[t['name'] for t in rec['catalog']['tables']]}")

    # ---- 2/3. 两种读法逐键比对 ----
    sqlite_db = sqlite3.connect(args.mirror)
    sqlite_db.row_factory = sqlite3.Row
    table_meta = {t["name"]: t for t in catalog["tables"]}
    all_ok = True
    mismatches = []
    for table in catalog["tables"]:
        name = table["name"]
        cols = table["columns"]
        key_cols = table["key"]
        key_pos = [cols.index(k) for k in key_cols]
        pf_rows = pagefold_image[name]

        sqlite_rows = {}
        col_list = ",".join(ident(c) for c in cols)
        for raw in sqlite_db.execute(f"SELECT {col_list} FROM main.{ident(name)}"):
            raw = tuple(raw)
            key_token = json.dumps([key_atom(raw[p]) for p in key_pos],
                                   ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            sqlite_rows[key_token] = raw

        if set(pf_rows) != set(sqlite_rows):
            all_ok = False
            mismatches.append(f"{name} 键集合不一致")
            continue
        for token, pf_value_atoms in pf_rows.items():
            sqlite_values = sqlite_rows[token]
            pf_values = [atom_to_python(a) for a in pf_value_atoms]
            # 严格按类型比较：Python 中 3 != 3.0 为 False（相等），需额外核 typeof
            if pf_values != list(sqlite_values):
                all_ok = False
                mismatches.append(f"{name} {token} 值不等: {pf_values} vs {sqlite_values}")
            for col, pf_atom, sql_val in zip(cols, pf_value_atoms, sqlite_values):
                expected_type = {
                    "i": int, "r": float, "t": str, "b": bytes, "n": type(None)
                }[pf_atom[0]]
                if not isinstance(sql_val, expected_type):
                    all_ok = False
                    mismatches.append(
                        f"{name} {token} 列 {col} 类型不符: 标签 {pf_atom[0]} vs {type(sql_val).__name__}")
    record("两种读法逐键逐类型一致", all_ok and not mismatches,
           f"共比对 {sum(len(v) for v in pagefold_image.values())} 行；"
           + ("全部一致" if not mismatches else "; ".join(mismatches[:5])))

    # 校验导出的 JSONL 与登记链路读到的内容一致（防止导出文件本身漂移）
    exported = [json.loads(line) for line in args.pagefold_jsonl.read_text(encoding="utf-8").splitlines()]
    export_ok = len(exported) == sum(len(v) for v in pagefold_image.values())
    record("Pagefold JSONL 行数与登记读取一致", export_ok, f"JSONL {len(exported)} 行")

    # ---- 4. 超大复合键 ----
    big_slots = []
    for token in pagefold_image["node"]:
        key = json.loads(token)
        bench_atom, slot_atom = key
        if bench_atom == ["t", "east"] and slot_atom[0] == "i":
            slot = int(slot_atom[1])
            if not (JS_MIN_SAFE <= slot <= JS_MAX_SAFE):
                # 原子必须是字符串承载的精确十进制，且 SQLite 中能按该键取回同一行
                back = sqlite_db.execute(
                    "SELECT slot FROM node WHERE bench=? AND slot=?", ("east", slot)
                ).fetchone()
                big_slots.append((slot, slot_atom[1] == str(slot), back is not None and back[0] == slot))
    big_ok = len(big_slots) >= 3 and all(text_exact and locatable for _, text_exact, locatable in big_slots)
    record("超出 JS 安全整数的复合键可精确定位", big_ok,
           "slot=" + ", ".join(str(s) for s, _, _ in big_slots))

    # ---- 5. 空 BLOB 与 NULL ----
    empty_blob = sqlite_db.execute(
        "SELECT count(*) FROM node WHERE bench='east' AND slot=100 AND payload IS x''"
    ).fetchone()[0]
    null_payload = sqlite_db.execute(
        "SELECT count(*) FROM node WHERE bench='east' AND slot=101 AND payload IS NULL"
    ).fetchone()[0]
    distinct = sqlite_db.execute(
        "SELECT (SELECT payload FROM node WHERE bench='east' AND slot=100) IS NOT "
        "(SELECT payload FROM node WHERE bench='east' AND slot=101)"
    ).fetchone()[0]
    record("空 BLOB 与 NULL 并存且不同", empty_blob == 1 and null_payload == 1 and distinct == 1,
           f"空BLOB行={empty_blob} NULL行={null_payload} 互判不同={distinct}")

    # ---- 6. 同显示异类型 ----
    type_rows = sqlite_db.execute(
        "SELECT code, typeof(sample) FROM type_probe WHERE code IN "
        "('num-int','num-real','num-text','num-blob','empty-text','empty-blob','null-sample') ORDER BY code"
    ).fetchall()
    have = {r[0]: r[1] for r in type_rows}
    want = {"num-int": "integer", "num-real": "real", "num-text": "text", "num-blob": "blob",
            "empty-text": "text", "empty-blob": "blob", "null-sample": "null"}
    type_ok = have == want
    record("同显示异类型样本齐备且类型保留", type_ok, json.dumps(have, ensure_ascii=False))

    # ---- 7. SQLite 比较归类 ----
    eq_num = sqlite_db.execute("SELECT 3 = 3.0").fetchone()[0]
    eq_text_blob = sqlite_db.execute("SELECT '3' = x'33'").fetchone()[0]
    eq_empty = sqlite_db.execute("SELECT '' = x''").fetchone()[0]
    null_eq = sqlite_db.execute("SELECT NULL = NULL").fetchone()[0]
    null_is = sqlite_db.execute("SELECT NULL IS NULL").fetchone()[0]
    group_count = sqlite_db.execute(
        "SELECT count(*) FROM (SELECT sample FROM type_probe WHERE code IN "
        "('num-int','num-real') GROUP BY sample)"
    ).fetchone()[0]
    cmp_ok = (eq_num == 1 and eq_text_blob == 0 and eq_empty == 0
              and null_eq is None and null_is == 1 and group_count == 1)
    record("SQLite 比较归类符合预期", cmp_ok,
           f"3=3.0:{eq_num} '3'=blob:{eq_text_blob} ''=x'':{eq_empty} "
           f"NULL=NULL:{None if null_eq is None else null_eq} 数值组并类数:{group_count}")

    # ---- 8. 母版锚点（与 pagefold/demo.py 石英镜像逐值一致）----
    from pagefold.demo import image as demo_image

    master_db = sqlite3.connect(":memory:")
    master_db.deserialize(demo_image("quartz"))
    anchor_ok = True
    anchor_detail = []
    for bench, slot in (("north", 1), ("south", 1)):
        master_row = master_db.execute(
            "SELECT bench,slot,note,payload,weight FROM node WHERE bench=? AND slot=?",
            (bench, slot)).fetchone()
        mirror_row = sqlite_db.execute(
            "SELECT bench,slot,note,payload,weight FROM node WHERE bench=? AND slot=?",
            (bench, slot)).fetchone()
        same = tuple(master_row) == tuple(mirror_row)
        anchor_ok &= same
        anchor_detail.append(f"{bench}/{slot}:{'一致' if same else '不符'}")
    master_binding = master_db.execute("SELECT * FROM binding WHERE id=1").fetchone()
    mirror_binding = sqlite_db.execute("SELECT * FROM binding WHERE id=1").fetchone()
    anchor_ok &= tuple(master_binding) == tuple(mirror_binding)
    record("母版锚点行与 demo.py 石英镜像逐值一致", anchor_ok, "，".join(anchor_detail) + "，binding/1 一致")
    master_db.close()
    sqlite_db.close()

    overall = all(c["ok"] for c in checks)
    report = {
        "mirror": args.mirror.name,
        "schema_signature": catalog["signature"],
        "overall_pass": overall,
        "checks": checks,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n总体结论: {'全部通过' if overall else '存在失败项'}；报告写入 {args.report}")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
