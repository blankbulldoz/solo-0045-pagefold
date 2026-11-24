import json
import sqlite3

from . import lineage
from .catalog import describe
from .codec import canonical, digest
from .constraints import check_database
from .errors import Problem
from .rows import read_image, read_table
from .sqlite_image import mounted


def register(store, data, label, claims=None, missing_note=None):
    checksum = digest(data)
    try:
        with mounted(data) as image:
            catalog = describe(image)
            check_database(image)
            rows = read_image(image, catalog)
    except (sqlite3.Error, UnicodeError) as exc:
        raise Problem("image_invalid", f"SQLite 无法读取镜像：{exc}") from exc
    for table in catalog["tables"]:
        table["rows"] = len(rows[table["name"]])
    with store.connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO images VALUES(?,?,?,?)",
                   (checksum, label, canonical(catalog), data))
        record = image_record(get_image(db, checksum))
        import_id = lineage.record_import(db, checksum, label)
        if claims:
            lineage.record_declared(db, checksum, import_id, claims)
        if missing_note:
            lineage.record_missing(db, checksum, import_id, missing_note)
        filename_hints = lineage.record_filename_hints(db, checksum, label, import_id)
        sealed = lineage.discover_sealed_outputs(db, checksum)
    record["import_id"] = import_id
    record["lineage"] = {"sealed_matches": sealed,
                         "filename_hints": filename_hints,
                         "declared_sources": list(claims or []),
                         "missing_note": missing_note}
    return record


def get_image(db, image_id, body=False):
    columns = "id,label,catalog,body" if body else "id,label,catalog,length(body) AS size"
    row = db.execute(f"SELECT {columns} FROM images WHERE id=?", (image_id,)).fetchone()
    if row is None:
        raise Problem("unknown_image", "找不到指定的镜像。", 404)
    return row


def image_record(row):
    return {"id": row["id"], "label": row["label"], "size": row["size"],
            "catalog": json.loads(row["catalog"])}


def list_images(db):
    return [dict(row) for row in db.execute("SELECT id,label,length(body) AS size FROM images ORDER BY rowid DESC")]


def table_page(db, image_id, name, offset):
    image = get_image(db, image_id, body=True)
    table = next((t for t in json.loads(image["catalog"])["tables"] if t["name"] == name), None)
    if table is None:
        raise Problem("unknown_table", "镜像中没有该表。", 404)
    with mounted(image["body"]) as snapshot:
        rows = read_table(snapshot, table)
    keys = sorted(rows)
    return {"table": table, "offset": offset, "total": len(keys),
            "rows": [{"key": json.loads(key), "values": rows[key]} for key in keys[offset:offset + 50]]}
