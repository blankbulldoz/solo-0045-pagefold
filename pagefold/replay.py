import sqlite3
from collections import defaultdict

from .blockers import Report, collect_foreign_keys
from .codec import canonical, unpack
from .errors import Problem
from .merge import selected
from .rows import read_image
from .sqlite_image import ident, mounted, validate_header


def amendments(items, decisions):
    changed = defaultdict(list)
    for item in items:
        role, row = selected(item, decisions)
        if role is None:
            raise Problem("unresolved", "存在未裁决的行，请先完成裁决。", 409)
        if row != item["base"]:
            changed[item["table"]].append((item, row))
    return changed


def apply_rows(db, table, operations, report=None, attempted=None):
    name = ident(table["name"])
    predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    for item, _ in operations:
        if item["base"] is not None:
            db.execute(f"DELETE FROM main.{name} WHERE {predicate}", tuple(map(unpack, item["key"])))
    for item, row in operations:
        token = canonical(item["key"])
        if attempted is not None:
            attempted.add((table["name"], token))
        if row is None:
            continue
        try:
            db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})",
                       tuple(map(unpack, row)))
        except sqlite3.IntegrityError as exc:
            if report is None:
                # 兼容旧调用方：没有收集器时保持首个错误即抛出的行为。
                raise Problem("constraint", f"{table['name']} {token}：{exc}") from exc
            # INSERT OR ABORT 只回滚当前语句，其余行仍可继续写入，
            # 因此同批里相互独立的约束问题可以一次收全。
            report.insert_failure(table, item, row, exc)


def rebuild(ancestor, catalog, operations):
    report = Report(catalog)
    attempted = set()
    with mounted(ancestor, writable=True) as db:
        expected = read_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        aborted = None
        for table in catalog["tables"]:
            changes = operations.get(table["name"], [])
            try:
                apply_rows(db, table, changes, report, attempted)
            except sqlite3.Error as exc:
                # DELETE 或非 IntegrityError 中断：剩余条目标为无法继续检查。
                report.abort(exc, operations, attempted)
                aborted = exc
                break
            for item, row in changes:
                key = canonical(item["key"])
                if row is None:
                    expected[table["name"]].pop(key, None)
                else:
                    expected[table["name"]][key] = row

        if aborted is None:
            try:
                integrity = db.execute("PRAGMA main.integrity_check(1)").fetchone()[0]
                if integrity != "ok":
                    report.integrity_failure(integrity)
            except sqlite3.Error as exc:
                report.check_unavailable("PRAGMA integrity_check", exc)
            try:
                collect_foreign_keys(db, report)
            except sqlite3.Error as exc:
                report.check_unavailable("PRAGMA foreign_key_check", exc)

        blockers = report.finalize(expected)
        if aborted is not None or blockers["verified"]:
            code, detail = report.primary_problem(blockers)
            raise Problem(code, detail, blockers=blockers)

        if read_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与计划目标不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data
