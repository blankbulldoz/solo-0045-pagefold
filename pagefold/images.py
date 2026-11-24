import json
import sqlite3

from .catalog import describe
from .codec import canonical, digest
from .constraints import check_database
from .errors import Problem
from .identity import (content_identity, equivalent_images, equivalent_usage,
                       image_usage, type_lookalikes)
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
    content_digest = content_identity(catalog["signature"], rows)
    with store.connect(write=True) as db:
        reused = db.execute("SELECT 1 FROM images WHERE id=?", (checksum,)).fetchone() is not None
        db.execute("INSERT OR IGNORE INTO images(id,label,catalog,body,content_digest) "
                   "VALUES(?,?,?,?,?)",
                   (checksum, label, canonical(catalog), data, content_digest))
        record = image_record(get_image(db, checksum))
        record["reused"] = reused
        record["equivalents"] = equivalent_images(db, checksum, content_digest)
        return record


def get_image(db, image_id, body=False):
    if body:
        columns = "id,label,catalog,content_digest,body,length(body) AS size"
    else:
        columns = "id,label,catalog,content_digest,length(body) AS size"
    row = db.execute(f"SELECT {columns} FROM images WHERE id=?", (image_id,)).fetchone()
    if row is None:
        raise Problem("unknown_image", "找不到指定的镜像。", 404)
    return row


def image_record(row):
    content_digest = row["content_digest"]
    identity = {"file_digest": row["id"], "content_digest": content_digest,
                "logical_digest": content_digest, "data_digest": content_digest}
    return {"id": row["id"], "file_digest": row["id"], "content_digest": content_digest,
            "logical_digest": content_digest, "data_digest": content_digest,
            "identity": identity,
            "label": row["label"], "size": row["size"],
            "catalog": json.loads(row["catalog"])}


def image_dossier(db, image_id):
    row = get_image(db, image_id, body=True)
    record = image_record(row)
    catalog = record["catalog"]
    with mounted(row["body"]) as snapshot:
        rows = read_image(snapshot, catalog)
    if not record["content_digest"]:
        record["content_digest"] = content_identity(catalog["signature"], rows)
        for alias in ("logical_digest", "data_digest"):
            record[alias] = record["content_digest"]
            record["identity"][alias] = record["content_digest"]
        record["identity"]["content_digest"] = record["content_digest"]
    record["identity"]["schema_digest"] = catalog["signature"]
    record["equivalents"] = equivalent_images(db, image_id, record["content_digest"])
    record["type_lookalikes"] = type_lookalikes(db, image_id, record, catalog, rows)
    record["usage"] = image_usage(db, image_id)
    record["equivalent_usage"] = equivalent_usage(db, record["equivalents"])
    return record


def identity_summary(db, image_id):
    dossier = image_dossier(db, image_id)
    return {"id": dossier["id"], "content_digest": dossier["content_digest"],
            "identity": dossier["identity"], "equivalents": dossier["equivalents"],
            "type_lookalikes": dossier["type_lookalikes"], "usage": dossier["usage"],
            "equivalent_usage": dossier["equivalent_usage"]}


def list_images(db):
    return [dict(row) for row in db.execute(
        "SELECT id,label,content_digest,length(body) AS size FROM images ORDER BY rowid DESC")]


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
