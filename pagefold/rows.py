"""Typed row scanning over a mounted SQLite image.

Key identity is computed once here (:class:`pagefold.keys.Key`), so plans,
pages and replay consume the same objects instead of re-parsing serialized
keys.  ``scan`` never renames or reorders columns: the projection and key
positions come from the catalog's :class:`Table`.
"""

from .errors import Problem
from .keys import Key, Row
from .sqlite_image import ident

MAX_ROWS = 20_000


def scan(db, table, allowance=MAX_ROWS):
    """Read one table as ``{Key: Row}`` with duplicate and NULL key checks."""
    result = {}
    positions = table.key_positions()
    cursor = db.execute(f"SELECT {table.projection()} FROM main.{ident(table.name)} LIMIT ?",
                        (allowance + 1,))
    for raw in cursor:
        if len(result) >= allowance:
            raise Problem("row_limit", "镜像超过 20,000 行上限。")
        key = Key.of([raw[position] for position in positions])
        if key.has_null:
            raise Problem("null_key", f"表 {table.name} 含有 NULL 主键。")
        if key in result:
            raise Problem("duplicate_key", f"表 {table.name} 含有重复主键。")
        result[key] = Row.of(raw)
    return result


def scan_image(db, catalog):
    """Read every catalog table, enforcing the image-wide row allowance."""
    result = {}
    remaining = MAX_ROWS
    for table in catalog.tables:
        rows = scan(db, table, remaining)
        result[table.name] = rows
        remaining -= len(rows)
    return result
