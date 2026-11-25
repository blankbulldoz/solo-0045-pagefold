from .codec import fingerprint
from .errors import Problem
from .sqlite_image import ident


def relation(db, name):
    fields = db.execute(f"PRAGMA main.table_xinfo({ident(name)})").fetchall()
    position = sorted((field[5], field[1]) for field in fields if field[5])
    if not position or any(field[6] for field in fields):
        raise Problem("table_shape", f"表 {name} 需要显式主键，并且所有列须可写入。")
    unique_keys = []
    unique_indexes = []
    for index_seq, index_name, is_unique, origin, partial in db.execute(
            f"PRAGMA main.index_list({ident(name)})"):
        if origin != "pk":
            continue
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        if any(part[5] and part[4].upper() != "BINARY" for part in parts):
            raise Problem("key_collation", f"表 {name} 的主键须使用 BINARY 比较。")
        if is_unique:
            columns = [part[2] for part in sorted(parts, key=lambda part: part[0])
                       if part[5] and part[2] is not None]
            if columns:
                unique_keys.append(columns)
                unique_indexes.append({"name": index_name, "columns": columns,
                                        "origin": origin, "partial": bool(partial)})
    # Explicit UNIQUE constraints and plain unique indexes are candidate entity
    # keys. Expression indexes are intentionally omitted from column lists.
    for index_seq, index_name, is_unique, origin, partial in db.execute(
            f"PRAGMA main.index_list({ident(name)})"):
        if not is_unique or origin == "pk":
            continue
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        columns = [part[2] for part in sorted(parts, key=lambda part: part[0])
                   if part[5] and part[2] is not None]
        if columns and len(columns) == sum(1 for part in parts if part[5]):
            unique_keys.append(columns)
            unique_indexes.append({"name": index_name, "columns": columns,
                                  "origin": origin, "partial": bool(partial)})
    foreign = db.execute(f"PRAGMA main.foreign_key_list({ident(name)})").fetchall()
    return {"name": name, "columns": [f[1] for f in fields], "types": [f[2] for f in fields],
            "key": [col for _, col in position], "unique_keys": unique_keys,
            "unique_indexes": unique_indexes,
            "foreign_keys": [list(row) for row in foreign]}


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
    catalog = {"signature": fingerprint(schemas), "tables": [relation(db, name) for name, _ in tables]}
    table_by_name = {table["name"]: table for table in catalog["tables"]}
    for table in catalog["tables"]:
        grouped = {}
        for row in table["foreign_keys"]:
            grouped.setdefault(row[0], []).append(row)
        constraints = []
        for constraint_id, rows in sorted(grouped.items()):
            rows = sorted(rows, key=lambda row: row[1])
            parent = table_by_name.get(rows[0][2])
            parent_key = parent["key"] if parent else []
            columns = [row[3] for row in rows]
            parent_columns = [row[4] or (parent_key[row[1]] if row[1] < len(parent_key) else None)
                              for row in rows]
            constraints.append({"id": constraint_id, "columns": columns,
                                "parent": rows[0][2], "parent_columns": parent_columns,
                                "on_update": rows[0][5], "on_delete": rows[0][6],
                                "match": rows[0][7]})
        table["foreign_key_constraints"] = constraints
        # Short alias for callers that think in terms of outgoing references.
        table["references"] = constraints
    return catalog
