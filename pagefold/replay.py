import sqlite3
from collections import defaultdict

from .codec import canonical, unpack
from .constraints import check_database
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


def delete_statement(table):
    predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
    return f"DELETE FROM main.{ident(table['name'])} WHERE {predicate}"


def insert_statement(table):
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    return f"INSERT OR ABORT INTO main.{ident(table['name'])} ({columns}) VALUES ({params})"


def apply_rows(db, table, operations):
    for item, _ in operations:
        if item["base"] is not None:
            db.execute(delete_statement(table), tuple(map(unpack, item["key"])))
    for item, row in operations:
        if row is not None:
            try:
                db.execute(insert_statement(table), tuple(map(unpack, row)))
            except sqlite3.IntegrityError as exc:
                raise Problem("constraint", f"{table['name']} {canonical(item['key'])}：{exc}") from exc


def rebuild(ancestor, catalog, operations):
    with mounted(ancestor, writable=True) as db:
        expected = read_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for table in catalog["tables"]:
            name = table["name"]
            changes = operations.get(name, [])
            apply_rows(db, table, changes)
            for item, row in changes:
                key = canonical(item["key"])
                if row is None:
                    expected[name].pop(key, None)
                else:
                    expected[name][key] = row
        check_database(db)
        if read_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与计划目标不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data
