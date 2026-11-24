from .codec import fingerprint
from .errors import Problem
from .sqlite_image import ident


def relation(db, name):
    fields = db.execute(f"PRAGMA main.table_xinfo({ident(name)})").fetchall()
    position = sorted((field[5], field[1]) for field in fields if field[5])
    if not position or any(field[6] for field in fields):
        raise Problem("table_shape", f"表 {name} 需要显式主键，并且所有列须可写入。")
    for _, index_name, _, origin, _ in db.execute(f"PRAGMA main.index_list({ident(name)})"):
        if origin != "pk":
            continue
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        if any(part[5] and part[4].upper() != "BINARY" for part in parts):
            raise Problem("key_collation", f"表 {name} 的主键须使用 BINARY 比较。")
    foreign = db.execute(f"PRAGMA main.foreign_key_list({ident(name)})").fetchall()
    unique_keys = []
    pk_index = next((index_name for _, index_name, _, origin, _ in
                     db.execute(f"PRAGMA main.index_list({ident(name)})") if origin == "pk"), None)
    pk_parts = db.execute(f"PRAGMA main.index_xinfo({ident(pk_index)})").fetchall() if pk_index else []
    pk_signature = [(part[1], part[4]) for part in sorted(pk_parts, key=lambda part: part[0])]
    for _, index_name, unique, origin, partial in db.execute(
            f"PRAGMA main.index_list({ident(name)})"):
        if not unique or origin == "pk" or partial:
            continue
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        columns = [part for part in parts if part[1] >= 0]
        if not columns:
            continue
        signature = [(part[1], part[4]) for part in sorted(columns, key=lambda part: part[0])]
        if signature == pk_signature:
            continue
        key_parts = [part for part in columns if part[5]] or columns
        unique_keys.append([part[2] for part in sorted(key_parts, key=lambda part: part[0])])
    return {"name": name, "columns": [f[1] for f in fields], "types": [f[2] for f in fields],
            "key": [col for _, col in position], "foreign_keys": [list(row) for row in foreign],
            "unique_keys": unique_keys}


def describe(db):
    schemas = db.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
                         "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name").fetchall()
    listed = {r[1]: r[2] for r in db.execute("PRAGMA main.table_list") if r[0] == "main"}
    if any(kind not in {"table", "index"} or (kind == "table" and listed.get(name) != "table")
           for kind, name, _, _ in schemas):
        raise Problem("schema_objects", "镜像只接受普通表和索引。")
    tables = [(name, sql) for kind, name, _, sql in schemas if kind == "table"]
    if not 1 <= len(tables) <= 32:
        raise Problem("table_count", "镜像须包含 1 至 32 张表。")
    if db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone():
        raise Problem("sequence", "镜像含有当前不支持的自动递增序列。")
    return {"signature": fingerprint(schemas), "tables": [relation(db, name) for name, _ in tables]}
