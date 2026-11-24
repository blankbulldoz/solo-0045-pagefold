from .codec import fingerprint
from .errors import Problem
from .sqlite_image import ident


def relation(db, name):
    fields = db.execute(f"PRAGMA main.table_xinfo({ident(name)})").fetchall()
    position = sorted((field[5], field[1]) for field in fields if field[5])
    if not position or any(field[6] for field in fields):
        raise Problem("table_shape", f"表 {name} 需要显式主键，并且所有列须可写入。")
    indexes = []
    for _, index_name, unique, origin, partial in db.execute(f"PRAGMA main.index_list({ident(name)})"):
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        if origin == "pk":
            if any(part[5] and part[4].upper() != "BINARY" for part in parts):
                raise Problem("key_collation", f"表 {name} 的主键须使用 BINARY 比较。")
            continue
        key_parts = sorted((part for part in parts if part[5]), key=lambda part: part[0])
        indexes.append({"name": index_name, "origin": origin, "unique": bool(unique),
                        "partial": bool(partial), "expression": any(part[1] < 0 for part in key_parts),
                        "columns": [part[2] for part in key_parts]})
    foreign = db.execute(f"PRAGMA main.foreign_key_list({ident(name)})").fetchall()
    return {"name": name, "columns": [f[1] for f in fields], "types": [f[2] for f in fields],
            "notnull": [bool(f[3]) for f in fields], "defaults": [f[4] for f in fields],
            "key": [col for _, col in position], "foreign_keys": [list(row) for row in foreign],
            "indexes": indexes}


def describe(db):
    schemas = db.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
                         "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name").fetchall()
    listed = {r[1]: (r[2], bool(r[4]), bool(r[5])) for r in db.execute("PRAGMA main.table_list")
              if r[0] == "main"}
    if any(kind not in {"table", "index"} or (kind == "table" and listed.get(name, (None,))[0] != "table")
           for kind, name, _, _ in schemas):
        raise Problem("schema_objects", "镜像只接受普通表和索引。")
    tables = [(name, sql) for kind, name, _, sql in schemas if kind == "table"]
    if not 1 <= len(tables) <= 32:
        raise Problem("table_count", "镜像须包含 1 至 32 张表。")
    if db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone():
        raise Problem("sequence", "镜像含有当前不支持的自动递增序列。")
    relations = []
    for name, _ in tables:
        entry = relation(db, name)
        _, entry["without_rowid"], entry["strict"] = listed[name]
        relations.append(entry)
    return {"signature": fingerprint(schemas), "tables": relations}
