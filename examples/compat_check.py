"""复用 Pagefold 自身判定逻辑的镜像兼容检查与三方模式比对。

用法：
    python3 -m examples.compat_check 镜像.sqlite [...]
    python3 -m examples.compat_check --compare 祖本.sqlite 左支.sqlite 右支.sqlite
    python3 -m examples.compat_check --selftest
    python3 -m examples.compat_check --json 镜像.sqlite

退出码：
    0  全部通过（单个镜像被接受，或三方模式记录一致）
    1  至少一个镜像在登记阶段被拒绝
    2  三方模式记录不一致；或自检未通过
    3  文件无法按请求读取
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pagefold.catalog import describe
from pagefold.constraints import check_database
from pagefold.errors import Problem
from pagefold.rows import read_image
from pagefold.sqlite_image import MAX_BYTES, ident, mounted, validate_header

STAGES = ("file", "schema", "integrity", "rows")


def inspect(data):
    """按登记顺序走四个阶段，返回首个拒绝点（与 images.register 一致）。"""
    report = {"size": len(data), "stages": [], "accepted": False,
              "signature": None, "tables": [], "total_rows": None, "problems": []}

    def reject(stage, exc):
        report["stages"].append({"stage": stage, "status": "rejected",
                                 "code": exc.code, "detail": exc.detail})
        report["problems"].append({"stage": stage, "code": exc.code, "detail": exc.detail})

    try:
        validate_header(data)
    except Problem as exc:
        reject("file", exc)
        return report
    report["stages"].append({"stage": "file", "status": "ok"})

    try:
        with mounted(data) as db:
            try:
                catalog = describe(db)
            except Problem as exc:
                reject("schema", exc)
                return report
            report["stages"].append({"stage": "schema", "status": "ok"})
            report["signature"] = catalog["signature"]
            report["tables"] = catalog["tables"]
            try:
                check_database(db)
            except Problem as exc:
                reject("integrity", exc)
                return report
            report["stages"].append({"stage": "integrity", "status": "ok"})
            try:
                rows = read_image(db, catalog)
            except Problem as exc:
                reject("rows", exc)
                return report
            report["stages"].append({"stage": "rows", "status": "ok"})
            for table in report["tables"]:
                table["rows"] = len(rows[table["name"]])
            report["total_rows"] = sum(len(value) for value in rows.values())
    except (sqlite3.Error, UnicodeError) as exc:
        problem = Problem("image_invalid", f"SQLite 无法读取镜像：{exc}")
        reject("file", problem)
        return report
    report["accepted"] = True
    return report


def probe(data):
    """在只读挂载上采集原始结构事实，即使镜像会被登记拒绝也尽量返回。"""
    facts = {"schema_rows": [], "tables": [], "header": header(data)}
    with mounted(data) as db:
        facts["schema_rows"] = [list(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
            "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name")]
        names = [row[0] for row in db.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='table' "
            "AND substr(name,1,7) <> 'sqlite_' ORDER BY name")]
        for name in names:
            columns = db.execute(f"PRAGMA main.table_xinfo({ident(name)})").fetchall()
            entry = {"name": name,
                     "columns": [{"name": c[1], "type": c[2], "notnull": bool(c[3]),
                                  "pk": c[5], "hidden": c[6]} for c in columns],
                     "key": [c[1] for _, c in sorted((c[5], c) for c in columns if c[5])],
                     "pk_index": [], "indexes": []}
            for _, index_name, unique, origin, partial in db.execute(
                    f"PRAGMA main.index_list({ident(name)})"):
                parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
                detail = {"name": index_name, "origin": origin, "unique": bool(unique),
                          "partial": bool(partial),
                          "parts": [{"column": p[2], "collation": p[4],
                                     "desc": bool(p[3]), "key": bool(p[5])} for p in parts]}
                if origin == "pk":
                    entry["pk_index"].append(detail)
                else:
                    entry["indexes"].append(detail)
            facts["tables"].append(entry)
    return facts


def header(data):
    if len(data) < 100:
        return {"bytes_18_19": None, "page_size": None}
    size = int.from_bytes(data[16:18], "big") or 65536
    return {"page_size": size, "bytes_18_19": data[18:20].hex(),
            "journal_state": "rollback(可接受)" if data[18:20] == b"\x01\x01"
            else "WAL 或未完成检查点(会被拒绝)"}


def diff_probes(probes):
    """比较三份 sqlite_schema 记录，列出缺失与文本差异。"""
    labels = ["base", "left", "right"]
    objects = [{(row[0], row[1]): row for row in p["schema_rows"]} for p in probes]
    keys = sorted(set().union(*objects))
    differences = []
    for key in keys:
        present = [key in obj for obj in objects]
        if all(present):
            texts = [obj[key][3] for obj in objects]
            if len(set(texts)) == 1:
                continue
            differences.append({"object": list(key), "kind": "sql",
                                "detail": {label: text for label, text in zip(labels, texts)}})
        else:
            differences.append({"object": list(key), "kind": "presence",
                                "detail": {label: ("有" if has else "无")
                                           for label, has in zip(labels, present)}})
    return differences


def report_text(name, report, facts, show_details):
    lines = [f"镜像：{name}", f"大小：{report['size']} 字节"]
    if facts and facts.get("header", {}).get("page_size"):
        meta = facts["header"]
        lines.append(f"页大小：{meta['page_size']}  文件头 18–19 字节：{meta['bytes_18_19']}"
                     f"（{meta['journal_state']}）")
    if report["accepted"]:
        lines.append(f"结果：接受（四个登记阶段全部通过）")
        lines.append(f"模式签名：{report['signature']}")
        lines.append(f"行数合计：{report['total_rows']}")
        for table in report["tables"]:
            lines.append(f"  表 {table['name']}  主键顺序={table['key']}  行数={table['rows']}")
            lines.append(f"    列（声明类型）：{list(zip(table['columns'], table['types']))}")
        if show_details and facts:
            for entry in facts["tables"]:
                if entry["pk_index"]:
                    parts = entry["pk_index"][0]["parts"]
                    lines.append(f"  表 {entry['name']} 主键索引：" + "，".join(
                        f"{p['column']}:{p['collation']}" for p in parts if p["key"]))
                else:
                    lines.append(f"  表 {entry['name']} 主键索引：无（INTEGER PRIMARY KEY 别名）")
                for index in entry["indexes"]:
                    columns = "，".join(
                        f"{p['column'] or '表达式'}:{p['collation']}{' DESC' if p['desc'] else ''}"
                        for p in index["parts"] if p["key"])
                    lines.append(f"    独立索引 {index['name']}"
                                 f"{' UNIQUE' if index['unique'] else ''}"
                                 f"{' PARTIAL' if index['partial'] else ''}：{columns}")
    else:
        problem = report["problems"][0]
        lines.append(f"结果：拒绝于「{problem['stage']}」阶段，代码 {problem['code']}")
        lines.append(f"  说明：{problem['detail']}")
        passed = [s["stage"] for s in report["stages"] if s["status"] == "ok"]
        if passed:
            lines.append(f"  此前已通过：{', '.join(passed)}")
    return "\n".join(lines)


def load(path):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        print(f"无法读取 {path}：{exc}", file=sys.stderr)
        raise SystemExit(3)


def main():
    parser = argparse.ArgumentParser(description="Pagefold 镜像兼容检查")
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--compare", nargs=3, metavar=("BASE", "LEFT", "RIGHT"),
                        help="比较三份镜像的模式记录是否可创建合流计划")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--selftest", action="store_true", help="运行内置判定样例")
    parser.add_argument("--affinity", action="store_true",
                        help="穷举列亲和 × 存储类，确认重演不产生 row_conversion")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if args.affinity:
        sys.exit(affinity_sweep())

    if args.compare:
        items = [(label, load(path)) for label, path in
                 zip(("base", "left", "right"), args.compare)]
        reports = [inspect(data) for _, data in items]
        probes = []
        for _, data in items:
            try:
                probes.append(probe(data))
            except Problem:
                probes.append({"schema_rows": [], "tables": [], "header": header(data)})
        payload = {"reports": [dict(report, label=label) for (label, _), report in zip(items, reports)],
                   "differences": diff_probes(probes)}
        payload["mergeable"] = all(r["accepted"] for r in reports) and not payload["differences"]
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            for (label, _), report, facts in zip(items, reports, probes):
                if not args.quiet:
                    shown = dict(zip(("base", "left", "right"), args.compare))[label]
                    print(report_text(f"{label} = {shown}", report, facts, show_details=False))
                    print()
            if not all(r["accepted"] for r in reports):
                print("结论：至少一份镜像未通过登记，先修复登记问题再比对模式。")
            elif payload["differences"]:
                print("结论：可以各自登记，但不能创建三方计划（schema_mismatch）。差异：")
                for diff in payload["differences"]:
                    print(f"  {diff['kind']}  {diff['object']}")
                    for label, value in diff["detail"].items():
                        print(f"    {label}: {value}")
            else:
                print("结论：三份模式记录逐字节一致，可以创建三方计划。")
                print("      注意：模式一致不保证重演通过，唯一键/外键/检查约束仍可能在合流时冲突。")
        return 0 if payload["mergeable"] else (1 if not all(r["accepted"] for r in reports) else 2)

    if not args.paths:
        parser.error("请提供至少一份镜像，或使用 --compare / --selftest")

    reports = []
    for path in args.paths:
        data = load(path)
        report = inspect(data)
        try:
            facts = probe(data)
        except Problem:
            facts = None
        reports.append(report)
        if args.json:
            report["path"] = str(path)
            report["facts"] = facts
        else:
            print(report_text(str(path), report, facts, show_details=True))
            print()
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    return 0 if all(r["accepted"] for r in reports) else 1


def selftest():
    """构造最小样例，逐条验证手册中的判定。返回退出码。"""
    from pagefold.codec import atom
    from pagefold.rows import key_atom

    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))

    def build(schema, statements=()):
        db = sqlite3.connect(":memory:", isolation_level=None)
        try:
            db.executescript(schema)
            for statement in statements:
                db.execute(statement)
            return db.serialize()
        finally:
            db.close()

    def code_of(data):
        report = inspect(data)
        return None if report["accepted"] else report["problems"][0]["code"]

    def stage_of(data):
        report = inspect(data)
        return None if report["accepted"] else report["problems"][0]["stage"]

    def signature(data):
        return inspect(data)["signature"]

    good = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT);", ["INSERT INTO t VALUES('a','x')"])
    check("普通表被接受", inspect(good)["accepted"])
    round_trip_db = sqlite3.connect(":memory:")
    try:
        round_trip_db.deserialize(good)
        round_trip = round_trip_db.serialize()
    finally:
        round_trip_db.close()
    check("serialize 往返不改签名", signature(good) == signature(round_trip))

    nocase = build("CREATE TABLE t(a TEXT PRIMARY KEY COLLATE NOCASE, v TEXT);")
    check("NOCASE 主键在 schema 阶段被拒", stage_of(nocase) == "schema"
          and code_of(nocase) == "key_collation")
    table_nocase = build("CREATE TABLE t(a TEXT, v TEXT, PRIMARY KEY(a COLLATE NOCASE));")
    check("表级 COLLATE NOCASE 同样被拒", code_of(table_nocase) == "key_collation")
    binary = build("CREATE TABLE t(a TEXT COLLATE BINARY PRIMARY KEY, v TEXT);")
    check("显式 COLLATE BINARY 被接受", inspect(binary)["accepted"])
    check("显式 BINARY 改变签名", signature(binary) != signature(good))

    reordered = build("CREATE TABLE t(a TEXT, b TEXT, v TEXT, PRIMARY KEY(b,a));")
    facts = probe(reordered)
    check("主键顺序取声明顺序而非列顺序", facts["tables"][0]["key"] == ["b", "a"],
          str(facts["tables"][0]["key"]))
    forward = build("CREATE TABLE t(a TEXT, b TEXT, v TEXT, PRIMARY KEY(a,b));")
    check("主键顺序不同即签名不同", signature(reordered) != signature(forward))

    indexed = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT); CREATE INDEX ix ON t(v);")
    check("独立索引可登记", inspect(indexed)["accepted"])
    check("独立索引改变签名", signature(indexed) != signature(good))
    renamed = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT); CREATE INDEX iy ON t(v);")
    check("仅索引改名也改变签名", signature(renamed) != signature(indexed))
    partial = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT); CREATE INDEX ix ON t(v) WHERE v IS NOT NULL;")
    check("部分索引改变签名", signature(partial) != signature(indexed))
    spaced = build("CREATE TABLE t( a TEXT PRIMARY KEY , v TEXT );")
    check("空白差异即签名不同", signature(spaced) != signature(good))
    ifnot = build("CREATE TABLE IF NOT EXISTS t(a TEXT PRIMARY KEY, v TEXT);")
    check("仅 CREATE 头部被规范化，IF NOT EXISTS 不改变签名", signature(ifnot) == signature(good))
    lower_type = build("CREATE TABLE t(a text PRIMARY KEY, v TEXT);")
    check("列类型名大小写差异改变签名", signature(lower_type) != signature(good))
    lower_head = build("create table t(a TEXT PRIMARY KEY, v TEXT);")
    check("CREATE TABLE 关键字大小写被规范化", signature(lower_head) == signature(good))
    lower_pk = build("CREATE TABLE t(a TEXT primary key, v TEXT);")
    check("括号内 primary key 小写改变签名", signature(lower_pk) != signature(good))
    lower_index = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT); create index ix on t(v);")
    index_head = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT); CREATE INDEX ix on t(v);")
    check("CREATE INDEX 关键字大小写被规范化", signature(lower_index) == signature(index_head))

    wal = bytearray(good)
    wal[18:20] = b"\x02\x02"
    check("WAL/未检查点在 file 阶段被拒", stage_of(bytes(wal)) == "file"
          and code_of(bytes(wal)) == "image_journal")

    no_key = build("CREATE TABLE t(a TEXT UNIQUE, v TEXT);")
    check("缺少显式主键被拒", code_of(no_key) == "table_shape")
    autoincrement = build("CREATE TABLE t(a INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT);")
    check("AUTOINCREMENT 被拒", code_of(autoincrement) == "sequence")
    view = build("CREATE TABLE t(a TEXT PRIMARY KEY); CREATE VIEW v AS SELECT * FROM t;")
    check("视图被拒", code_of(view) == "schema_objects")

    null_key = build("CREATE TABLE t(a TEXT PRIMARY KEY, v TEXT);", ["INSERT INTO t VALUES(NULL,'x')"])
    check("rowid 表的 NULL 主键在 rows 阶段被拒", stage_of(null_key) == "rows"
          and code_of(null_key) == "null_key")
    infinite = build("CREATE TABLE t(a TEXT PRIMARY KEY, v REAL);", ["INSERT INTO t VALUES('a',9e999)"])
    check("非有限实数在 rows 阶段被拒", stage_of(infinite) == "rows"
          and code_of(infinite) == "unsupported_value")

    check("键把整数值实数归一为整数", key_atom(1.0) == ["i", "1"] and key_atom(1) == ["i", "1"])
    check("键保留文本与二进制的区分",
          key_atom("1") == ["t", "1"] and key_atom(b"1") == ["b", "MQ=="])
    check("-0.0 与 0 同一身份", key_atom(-0.0) == ["i", "0"])
    check("2**63 的实数不归一", key_atom(float(2**63))[0] == "r")
    check("非键单元格保留存储类", atom(1) != atom(1.0) and atom(1.0)[0] == "r")

    def stored(affinity, value):
        db = sqlite3.connect(":memory:", isolation_level=None)
        try:
            db.execute(f"CREATE TABLE t(x {affinity})")
            db.execute("INSERT INTO t VALUES(?)", (value,))
            return db.execute("SELECT typeof(x) FROM t").fetchone()[0]
        finally:
            db.close()

    check("TEXT 亲和把整数 1 存成文本", stored("TEXT", 1) == "text")
    check("NUMERIC 亲和把 1.0 存成整数", stored("NUMERIC", 1.0) == "integer")
    check("无亲和列保留整数与实数", stored("", 1) == "integer" and stored("", 1.0) == "real")
    check("BLOB 亲和保留文本", stored("BLOB", "1") == "text")

    def compare_mergeable(a, b):
        probes = [probe(x) for x in (a, a, b)]
        return not diff_probes(probes)

    check("同表不同索引不可三方合流", not compare_mergeable(good, indexed))
    check("逐字节相同可合流", compare_mergeable(good, good))

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(("✓ " if ok else "✗ ") + name + (("  [" + detail + "]") if detail and not ok else ""))
    print(f"\n自检：{len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if not failed else 2


def affinity_sweep():
    """穷举列亲和 × 存储类，确认重演不会因亲和转换报 row_conversion。

    每格构造三份镜像：base 为空、left 插入给定的 (键,值)、right 插入同一键但不同值，
    形成冲突项后强制选 left，真正走一遍 rebuild 的写入与回读比对。
    """
    from pagefold.catalog import describe
    from pagefold.merge import calculate
    from pagefold.replay import amendments, rebuild
    from pagefold.rows import read_image

    affinities = ["", "TEXT", "NUMERIC", "INTEGER", "REAL", "BLOB"]
    values = {"整数": 1, "整值实数": 1.0, "小数实数": 1.5, "数值文本": "1",
              "文本": "x", "二进制": b"\x01", "NULL": None}

    def image(schema, insertion=None):
        db = sqlite3.connect(":memory:", isolation_level=None)
        try:
            db.executescript(schema)
            if insertion:
                try:
                    db.execute(*insertion)
                except sqlite3.Error:
                    return None
            return db.serialize()
        finally:
            db.close()

    passed = skipped = operations = 0
    blocked = {}
    for affinity in affinities:
        for left_name, left_value in values.items():
            for right_name, right_value in values.items():
                schema = f"CREATE TABLE t(k {affinity}, v {affinity}, PRIMARY KEY(k))"
                base = image(schema)
                left = image(schema, ("INSERT INTO t VALUES(?,?)", (left_value, left_value)))
                right = image(schema, ("INSERT INTO t VALUES(?,?)", (right_value, "差异")))
                if base is None or left is None or right is None:
                    skipped += 1
                    continue
                try:
                    with mounted(base) as db:
                        catalog = describe(db)
                        snapshots = [read_image(db, catalog)]
                    for data in (left, right):
                        with mounted(data) as db:
                            snapshots.append(read_image(db, catalog))
                except Problem:
                    skipped += 1
                    continue
                items = calculate(*snapshots)
                decisions = {item["id"]: "left" for item in items if item["automatic"] is None}
                try:
                    changes = amendments(items, decisions)
                    operations += sum(map(len, changes.values()))
                    rebuild(base, catalog, changes)
                    passed += 1
                except Problem as exc:
                    blocked[exc.code] = blocked.get(exc.code, 0) + 1
                    print(f"  {affinity or '无亲和'} 键={left_name} 值={right_name}"
                          f" -> {exc.code}: {exc.detail[:120]}")
    total = passed + sum(blocked.values())
    print(f"亲和穷举：{passed}/{total} 组合重演通过，实际写入 {operations} 行，跳过 {skipped} 组")
    if blocked:
        print("非预期失败：", blocked)
    else:
        print("未出现 row_conversion；登记—重演的类型往返在常见亲和下自洽。")
    return 0 if not blocked else 2


if __name__ == "__main__":
    sys.exit(main())