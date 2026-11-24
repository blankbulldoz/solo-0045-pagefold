from .codec import fingerprint
from .errors import Problem
from .sqlite_image import ident


def index_order(db, table, index_name):
    # 末位字段 key：1 为用户声明的索引列，0 为 WITHOUT ROWID 表自动追加的主键后缀列，
    # 后缀列不属于声明的索引顺序；表达式列 name 为 NULL，保留占位并由 sql 给出完整定义
    parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
    ordered = []
    for part in sorted(parts, key=lambda part: part[0]):
        if part[-1] == 0:
            continue
        ordered.append(part[2] if part[2] is not None else f"表达式#{part[0] + 1}")
    return ordered


def indexes(db, table):
    found = []
    for seq, index_name, unique, origin, partial in db.execute(
            f"PRAGMA main.index_list({ident(table)})").fetchall():
        if origin == "pk":
            continue
        sql = db.execute("SELECT sql FROM main.sqlite_schema WHERE name=?", (index_name,)).fetchone()[0]
        found.append({"name": index_name, "unique": bool(unique), "origin": origin,
                      "partial": bool(partial), "columns": index_order(db, table, index_name),
                      "sql": sql or f"自动索引 {index_name}"})
    return sorted(found, key=lambda item: item["name"])


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
    all_indexes = indexes(db, name)
    return {"name": name,
            "columns": [{"name": f[1], "type": f[2], "notnull": bool(f[3]), "default": f[4]}
                        for f in fields],
            "key": [col for _, col in position], "foreign_keys": [list(row) for row in foreign],
            "indexes": [index for index in all_indexes if index["origin"] != "u"],
            "constraint_indexes": [index for index in all_indexes if index["origin"] == "u"]}


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
