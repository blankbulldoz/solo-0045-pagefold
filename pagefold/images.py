import json
import sqlite3

from .catalog import describe
from .codec import canonical, digest
from .constraints import check_database
from .errors import Problem
from .rows import read_image, read_table
from .sqlite_image import mounted


def prepare_image(data):
    """读取并完整校验一份镜像，但不写入状态库。"""
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
    return checksum, canonical(catalog)


def persist_image(db, checksum, label, catalog_text, data):
    """在已开启的写事务内登记镜像，已存在则复用同一摘要。"""
    db.execute("INSERT OR IGNORE INTO images VALUES(?,?,?,?)",
               (checksum, label, catalog_text, data))


def register(store, data, label):
    checksum, catalog_text = prepare_image(data)
    with store.connect(write=True) as db:
        persist_image(db, checksum, label[:100], catalog_text, data)
        return image_record(get_image(db, checksum))


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
