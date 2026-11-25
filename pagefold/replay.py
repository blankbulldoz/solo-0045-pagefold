"""Rebuild a candidate from the ancestor using typed plan items.

This module is the SQLite application boundary for replay: plan items arrive as
typed keys and rows, and ``Key``/``Row`` are converted to bindable values only
at ``execute`` call sites.
"""

import sqlite3

from .codec import canonical
from .constraints import check_database
from .errors import Problem
from .merge import selected
from .rows import scan_image
from .sqlite_image import ident, mounted, validate_header


def amendments(items, decisions):
    """Group row operations per table, keeping catalog order."""
    changed = {}
    for item in items:
        role, row = selected(item, decisions)
        if role is None:
            raise Problem("unresolved", "存在未裁决的行，请先完成裁决。", 409)
        if row != item.base:
            changed.setdefault(item.table, []).append((item, row))
    return changed


def apply_rows(db, table, operations):
    name = ident(table.name)
    for item, _ in operations:
        if item.base is not None:
            db.execute(f"DELETE FROM main.{name} WHERE {table.key_predicate()}",
                       tuple(value.sqlite() for value in item.key.values))
    for item, row in operations:
        if row is not None:
            try:
                db.execute(f"INSERT OR ABORT INTO main.{name} ({table.projection()}) "
                           f"VALUES ({table.placeholders()})",
                           tuple(value.sqlite() for value in row.values))
            except sqlite3.IntegrityError as exc:
                raise Problem("constraint", f"{table.name} {canonical(item.key.atoms())}：{exc}") from exc


def rebuild(ancestor, catalog, operations):
    with mounted(ancestor, writable=True) as db:
        expected = scan_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for table in catalog.tables:
            changes = operations.get(table.name, [])
            apply_rows(db, table, changes)
            for item, row in changes:
                if row is None:
                    expected[table.name].pop(item.key, None)
                else:
                    expected[table.name][item.key] = row
        check_database(db)
        if scan_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与计划目标不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data
