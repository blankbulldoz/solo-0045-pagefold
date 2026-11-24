import sqlite3

from .codec import canonical, unpack
from .constraints import check_database
from .errors import Problem
from .rows import read_image
from .sqlite_image import ident, mounted, validate_header


def apply_rows(db, table, operations):
    name = ident(table["name"])
    predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    for item, _ in operations:
        if item["base"] is not None:
            db.execute(f"DELETE FROM main.{name} WHERE {predicate}", tuple(map(unpack, item["key"])))
    for item, row in operations:
        if row is not None:
            try:
                db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})", tuple(map(unpack, row)))
            except sqlite3.IntegrityError as exc:
                raise Problem("constraint", f"{table['name']} {canonical(item['key'])}：{exc}") from exc


def rebuild(ancestor, catalog, operations):
    untouched = [table["name"] for table in catalog["tables"] if not operations.get(table["name"])]
    with mounted(ancestor) as reference:
        ancestor_snapshot = read_image(reference, catalog)
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
        actual = read_image(db, catalog)
        if actual != expected:
            raise Problem("row_conversion", "写入后读取到的行与计划目标不一致。")
        for name in untouched:
            if actual[name] != ancestor_snapshot[name]:
                raise Problem("untouched_table", f"范围外表 {name} 与祖本不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data
