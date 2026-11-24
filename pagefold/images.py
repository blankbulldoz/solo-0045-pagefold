import json
import sqlite3

from .catalog import describe
from .codec import canonical, digest
from .constraints import check_database
from .errors import Problem
from .rows import read_image, read_table
from .sqlite_image import mounted


def register(store, data, label):
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
        db.execute("""
            INSERT INTO images(id, label, catalog, body, size, reclaimed) VALUES(?,?,?,?,?,0)
            ON CONFLICT(id) DO UPDATE SET body=excluded.body, size=excluded.size, reclaimed=0
        """, (checksum, label, canonical(catalog), data, len(data)))
        return image_record(get_image(db, checksum))


def get_image(db, image_id, body=False):
    columns = "id,label,catalog,body,size,reclaimed" if body else "id,label,catalog,size,reclaimed"
    row = db.execute(f"SELECT {columns} FROM images WHERE id=?", (image_id,)).fetchone()
    if row is None:
        raise Problem("unknown_image", "找不到指定的镜像。", 404)
    if body and row["reclaimed"]:
        raise Problem("image_reclaimed", "该镜像的原始内容已回收，数据页不可读取。", 410)
    return row


def image_record(row):
    return {"id": row["id"], "label": row["label"], "size": row["size"],
            "reclaimed": bool(row["reclaimed"]), "catalog": json.loads(row["catalog"])}


def list_images(db):
    return [dict(row, reclaimed=bool(row["reclaimed"])) for row in db.execute(
        "SELECT id,label,size,reclaimed FROM images ORDER BY rowid DESC")]


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
