import base64
import binascii
import json
import math
import re
from bisect import bisect_left

from .codec import atom, canonical
from .errors import Problem
from .sqlite_image import ident

MAX_ROWS = 20_000
PAGE_ROWS = 50
INTEGER = re.compile(r"-?[0-9]+\Z")


def key_atom(value):
    if isinstance(value, float) and value.is_integer() and -(2**63) <= value < 2**63:
        return atom(int(value))
    return atom(value)


def key_value(value):
    """把线上传输的类型化原子还原为 Python 值，并复核其类型标签。"""
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str):
        raise Problem("key_atom", "主键的每个部分都须为 [类型, 值] 形式的类型化原子。", 400)
    tag, raw = value
    if tag == "n":
        raise Problem("null_key", "主键不能为 NULL。", 400)
    if tag == "i":
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise Problem("key_atom", "整数键须为十进制字符串或 JSON 整数。", 400)
        if isinstance(raw, str):
            if not INTEGER.fullmatch(raw):
                raise Problem("key_atom", "整数键须以十进制字符串传输。", 400)
            raw = int(raw)
        if not -(2**63) <= raw < 2**63:
            raise Problem("key_atom", "整数键超出 SQLite 的 64 位范围。", 400)
        return raw
    if tag == "r":
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise Problem("key_atom", "实数键须为十六进制浮点字符串或 JSON 数字。", 400)
        if isinstance(raw, str):
            if not raw.strip():
                raise Problem("key_atom", "实数键须以十六进制浮点字符串传输。", 400)
            try:
                raw = float.fromhex(raw.strip())
            except ValueError as exc:
                raise Problem("key_atom", "实数键无法按浮点格式读取。", 400) from exc
        else:
            raw = float(raw)
        if not math.isfinite(raw):
            raise Problem("key_atom", "实数键须为有限值。", 400)
        return raw
    if tag == "t":
        if not isinstance(raw, str):
            raise Problem("key_atom", "文本键须以字符串传输。", 400)
        return raw
    if tag == "b":
        if not isinstance(raw, str):
            raise Problem("key_atom", "二进制键须以 base64 字符串传输。", 400)
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise Problem("key_atom", "二进制键不是有效的 base64。", 400) from exc
    raise Problem("key_atom", "主键原子只接受 i、r、t、b 类型；NULL 不构成主键。", 400)


def typed_key(raw, table):
    """解析 URL 中的类型化复合键；不接受展示标签或裸字符串。"""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise Problem("key_shape", "key 须为 URL 编码的 JSON 类型化键。", 400)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise Problem("key_json", "key 不是有效的 JSON。", 400) from exc
    names = table["key"]
    if isinstance(value, dict):
        if set(value) != set(names):
            raise Problem("key_shape", "键对象须恰好包含 " + "、".join(names) + "。", 400)
        parts = [value[name] for name in names]
    elif isinstance(value, list):
        parts = value
    else:
        raise Problem("key_shape", "key 须为有序原子列表或按主键列命名的对象。", 400)
    if len(parts) != len(names):
        raise Problem("key_shape", f"表 {table['name']} 的主键需要 {len(names)} 个部分。", 400)
    return [key_atom(key_value(part)) for part in parts]


def locate(keys, key):
    """在现有浏览次序（规范化键 token 的排序）中定位键。"""
    token = canonical(key)
    position = bisect_left(keys, token)
    total = len(keys)
    found = position < total and keys[position] == token
    if total == 0:
        page = 0
    elif found or position < total:
        page = (position // PAGE_ROWS) * PAGE_ROWS
    else:
        page = ((total - 1) // PAGE_ROWS) * PAGE_ROWS

    def nearby(index):
        return {"key": json.loads(keys[index]), "position": index,
                "page": (index // PAGE_ROWS) * PAGE_ROWS}

    before = position - 1
    ahead = position + 1 if found else position
    previous = nearby(before) if before >= 0 else None
    following = nearby(ahead) if ahead < total else None
    return {"key": key, "token": token, "found": found, "position": position, "page": page,
            "previous": previous, "next": following}


def read_table(db, table, allowance=MAX_ROWS):
    name = table["name"]
    positions = [table["columns"].index(key) for key in table["key"]]
    cols = ",".join(map(ident, table["columns"]))
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
