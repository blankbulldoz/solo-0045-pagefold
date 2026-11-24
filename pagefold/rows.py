from .codec import atom, canonical
from .errors import Problem
from .sqlite_image import ident

MAX_ROWS = 20_000


def key_atom(value):
    if isinstance(value, float) and value.is_integer() and -(2**63) <= value < 2**63:
        return atom(int(value))
    return atom(value)


def read_table(db, table, allowance=MAX_ROWS):
    name = table["name"]
    column_names = [column["name"] for column in table["columns"]]
    positions = [column_names.index(key) for key in table["key"]]
    cols = ",".join(ident(name) for name in column_names)
    result = {}
    cursor = db.execute(f"SELECT {cols} FROM main.{ident(name)} LIMIT ?", (allowance + 1,))
    for raw in cursor:
        if len(result) >= allowance:
            raise Problem("row_limit", "镜像超过 20,000 行上限。")
        row = [atom(value) for value in raw]
        key = [key_atom(raw[pos]) for pos in positions]
        if any(part[0] == "n" for part in key):
            raise Problem("null_key", f"表 {name} 含有 NULL 主键。")
        token = canonical(key)
        if token in result:
            raise Problem("duplicate_key", f"表 {name} 含有重复主键。")
        result[token] = row
    return result


def read_image(db, catalog):
    result = {}
    remaining = MAX_ROWS
    for table in catalog["tables"]:
        rows = read_table(db, table, remaining)
        result[table["name"]] = rows
        remaining -= len(rows)
    return result
