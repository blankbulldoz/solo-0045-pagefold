"""Image registration and the typed row-window reader.

Registration parses the catalog once and stores its record; readers load the
record back into :class:`~pagefold.catalog.Catalog` so column identity and key
positions come from one validated structure.
"""

import json
import sqlite3

from .catalog import Catalog, describe
from .codec import canonical, digest
from .constraints import check_database
from .errors import Problem
from .rows import scan, scan_image
from .sqlite_image import mounted


def register(store, data, label):
    checksum = digest(data)
    try:
        with mounted(data) as image:
            catalog = describe(image)
            check_database(image)
            rows = scan_image(image, catalog)
    except (sqlite3.Error, UnicodeError) as exc:
        raise Problem("image_invalid", f"SQLite 无法读取镜像：{exc}") from exc
    catalog = catalog.with_row_counts({name: len(found) for name, found in rows.items()})
    with store.connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO images VALUES(?,?,?,?)",
                   (checksum, label, canonical(catalog.record()), data))
        return image_record(get_image(db, checksum))


def get_image(db, image_id, body=False):
    columns = "id,label,catalog,body" if body else "id,label,catalog,length(body) AS size"
    row = db.execute(f"SELECT {columns} FROM images WHERE id=?", (image_id,)).fetchone()
    if row is None:
        raise Problem("unknown_image", "找不到指定的镜像。", 404)
    return row


def image_catalog(row):
    """Load a stored catalog record into typed value objects."""
    return Catalog.from_record(json.loads(row["catalog"]))


def image_record(row):
    return {"id": row["id"], "label": row["label"], "size": row["size"],
            "catalog": json.loads(row["catalog"])}


def list_images(db):
    return [dict(row) for row in db.execute("SELECT id,label,length(body) AS size FROM images ORDER BY rowid DESC")]


def table_page(db, image_id, name, offset):
    image = get_image(db, image_id, body=True)
    catalog = image_catalog(image)
    table = catalog.table(name)
    if table is None:
        raise Problem("unknown_table", "镜像中没有该表。", 404)
    with mounted(image["body"]) as snapshot:
        rows = scan(snapshot, table)
    keys = sorted(rows, key=lambda key: key.token)
    return {"table": table.record(), "offset": offset, "total": len(keys),
            "rows": [{"key": key.atoms(), "values": rows[key].atoms()}
                     for key in keys[offset:offset + 50]]}
