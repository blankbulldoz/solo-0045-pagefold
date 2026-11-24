"""人工合成裁决行的类型转换、主键对齐与请求形态兼容。

JSON 行沿用 codec 的类型标记：``["i","1"]``、``["r", hex]``、``["t",""]``、
``["b", base64]``、``["n"]``。网页表单按列位置提交 ``mt:<i>``/``mv:<i>``。
转换在裁决写入前完成；任何失败都不推进计划版本。
"""

import base64
import binascii
import json
import math
import re

from .errors import Problem
from .rows import key_atom
from .codec import unpack

TAGS = ("n", "i", "r", "t", "b")
TYPE_LABELS = {"n": "NULL", "i": "整数", "r": "实数", "t": "文本", "b": "BLOB"}
TAG_ALIASES = {
    "n": "n", "null": "n", "none": "n", "nil": "n", "空": "n",
    "i": "i", "int": "i", "integer": "i", "整数": "i", "整型": "i",
    "r": "r", "real": "r", "float": "r", "double": "r", "实数": "r", "浮点": "r",
    "t": "t", "text": "t", "str": "t", "string": "t", "文本": "t", "字符串": "t",
    "b": "b", "blob": "b", "bytes": "b", "binary": "b", "二进制": "b",
}
INT_RE = re.compile(r"[+-]?[0-9]+")
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

# 调用方可能用来携带人工行的字段名（规范名是 row）。
ROW_FIELDS = ("row", "values", "manual_row", "custom_row", "synthetic_row", "synthesized_row",
              "composed_row", "manualValues", "manual_values", "manual", "custom", "synthetic",
              "synthesized", "composed", "composition", "cells", "data")
MANUAL_CHOICE_NAMES = {"manual", "custom", "synthetic", "synthesized", "composite", "composed",
                       "compose", "synthesize", "handmade", "人工", "人工合成", "合成行", "人工合成行"}


def normalize_choice_name(value):
    """把不同命名习惯的“人工合成”选择归一为 manual；无法识别返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in ("base", "left", "right", "clear", "manual"):
        return text
    if text.lower() in MANUAL_CHOICE_NAMES or text in MANUAL_CHOICE_NAMES:
        return "manual"
    return None


def has_row_payload(data):
    return isinstance(data, dict) and any(field in data for field in ROW_FIELDS)
TYPE_SUFFIXES = ("_type", "__type", "-type", ":type", "_kind", "_tag")
VALUE_PREFIXES = ("value_", "val_", "v_", "mv:")
TYPE_PREFIXES = ("type_", "kind_", "tag_", "mt:")


def conversion_failure(column, position, reason, item_id=None):
    info = {"stage": "decision_input", "step": "convert",
            "column": column, "position": position}
    if item_id is not None:
        info["item"] = item_id
    label = f"第 {position + 1} 列「{column}」" if column else f"第 {position + 1} 列"
    return Problem("manual_value", f"人工合成行{label}没有通过类型转换：{reason}", 422, info)


def missing_row(item_id=None):
    info = {"stage": "decision_input", "step": "manual_row"}
    if item_id is not None:
        info["item"] = item_id
    return Problem("manual_row",
                   "选择“人工合成”时必须随附 row：与表列对齐的类型化单元格数组。", 422, info)


def _check_int_range(value, column, position, item_id):
    if not INT64_MIN <= value <= INT64_MAX:
        raise conversion_failure(column, position,
                                f"整数 {value} 超出 SQLite 有符号 64 位范围。", item_id)
    return ["i", str(value)]


def _real_atom(value, column, position, item_id):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise conversion_failure(column, position, f"{value!r} 不是有限实数。", item_id)
    if not math.isfinite(number):
        raise conversion_failure(column, position, "实数必须有限，不能是 NaN 或无穷。", item_id)
    return ["r", number.hex()]


def _blob_atom(text, column, position, item_id):
    if not isinstance(text, str):
        raise conversion_failure(column, position, "BLOB 须为 base64 文本。", item_id)
    raw = re.sub(r"\s+", "", text)
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        try:  # 兼容 URL-safe base64。
            decoded = base64.b64decode(raw.replace("-", "+").replace("_", "/"), validate=True)
        except (binascii.Error, ValueError):
            raise conversion_failure(column, position,
                                     "BLOB 须是合法 base64（空 BLOB 用空字符串）。", item_id)
    return ["b", base64.b64encode(decoded).decode("ascii")]


def convert_cell(raw, column="", position=0, item_id=None):
    """把单个 JSON 入参单元格转换成规范 atom。"""
    if raw is None:
        return ["n"]
    if isinstance(raw, bool):
        return ["i", "1" if raw else "0"]
    if isinstance(raw, int):
        return _check_int_range(raw, column, position, item_id)
    if isinstance(raw, float):
        return _real_atom(raw, column, position, item_id)
    if isinstance(raw, str):
        # 未加标记的 JSON 字符串按文本处理；"" 因此是空文本而不是 NULL。
        return ["t", raw]
    if isinstance(raw, (list, tuple)) and raw:
        tag = raw[0]
        value = raw[1] if len(raw) > 1 else None
        if not isinstance(tag, str) or tag not in TAGS:
            raise conversion_failure(column, position,
                                     "类型标记须是 n、i、r、t、b 之一。", item_id)
        if tag == "n":
            return ["n"]
        if tag == "i":
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, int):
                return _check_int_range(value, column, position, item_id)
            if isinstance(value, float) and value.is_integer():
                return _check_int_range(int(value), column, position, item_id)
            if isinstance(value, str) and INT_RE.fullmatch(value.strip()):
                return _check_int_range(int(value.strip()), column, position, item_id)
            raise conversion_failure(column, position,
                                     f"{value!r} 不是整数；文本数字也须写成 [\"i\",\"12\"]。", item_id)
        if tag == "r":
            if isinstance(value, str):
                text = value.strip()
                try:
                    number = float.fromhex(text) if text.lower().startswith(("0x", "-0x", "+0x")) \
                        else float(text)
                except ValueError:
                    raise conversion_failure(column, position, f"{value!r} 不是实数。", item_id)
            else:
                number = value
            return _real_atom(number, column, position, item_id)
        if tag == "t":
            if not isinstance(value, str):
                raise conversion_failure(column, position, "文本值须是 JSON 字符串。", item_id)
            return ["t", value]
        if tag == "b":
            if isinstance(value, dict):
                if "base64" in value:
                    return _blob_atom(value["base64"], column, position, item_id)
                if "hex" in value:
                    try:
                        decoded = bytes.fromhex(re.sub(r"\s+", "", str(value["hex"])))
                    except ValueError:
                        raise conversion_failure(column, position, "hex 字段须是偶数位十六进制。", item_id)
                    return ["b", base64.b64encode(decoded).decode("ascii")]
            return _blob_atom(value, column, position, item_id)
    if isinstance(raw, dict):
        for tag in TAGS:
            if tag in raw:
                return convert_cell([tag, raw[tag]], column, position, item_id)
        if "base64" in raw:
            return convert_cell(["b", raw["base64"]], column, position, item_id)
        if "hex" in raw:
            try:
                decoded = bytes.fromhex(re.sub(r"\s+", "", str(raw["hex"])))
            except ValueError:
                raise conversion_failure(column, position, "hex 字段须是偶数位十六进制。", item_id)
            return ["b", base64.b64encode(decoded).decode("ascii")]
        kind = raw.get("type", raw.get("kind", raw.get("tag")))
        if isinstance(kind, str):
            tag = TAG_ALIASES.get(kind.strip()) or TAG_ALIASES.get(kind.strip().lower())
            if tag:
                value = raw.get("value", raw.get("v", raw.get("text")))
                return convert_cell([tag, value], column, position, item_id)
    raise conversion_failure(column, position,
                             "无法识别的单元格；请使用 [\"n\"]、[\"i\", …]、[\"r\", …]、"
                             "[\"t\", …] 或 [\"b\", base64]。", item_id)


def convert_typed_text(tag, text, column, position, item_id=None, declared=""):
    """网页表单路径：类型选择器 + 原始文本。"""
    if tag is None or tag == "":
        tag = affinity_tag(declared)
    tag_name = str(tag).strip()
    resolved = TAG_ALIASES.get(tag_name) or TAG_ALIASES.get(tag_name.lower())
    if resolved is None:
        raise conversion_failure(column, position, f"未知类型 {tag!r}。", item_id)
    if resolved == "n":
        return ["n"]
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise conversion_failure(column, position, "表单值须是文本。", item_id)
    if resolved == "t":
        return ["t", text]
    if resolved == "i":
        if not INT_RE.fullmatch(text.strip()):
            raise conversion_failure(column, position, f"{text!r} 不是整数。", item_id)
        return _check_int_range(int(text.strip()), column, position, item_id)
    if resolved == "r":
        return _real_atom(text.strip(), column, position, item_id)
    # 表单里额外允许 hex: 前缀，方便人工粘贴；JSON API 仍只认标准 base64。
    if text.strip().lower().startswith("hex:"):
        try:
            decoded = bytes.fromhex(text.strip()[4:])
        except ValueError:
            raise conversion_failure(column, position, "hex: 后须是偶数位十六进制。", item_id)
        return ["b", base64.b64encode(decoded).decode("ascii")]
    return _blob_atom(text, column, position, item_id)


def affinity_tag(declared):
    """未显式给类型时，按列声明类型推断（SQLite 类型亲缘）。"""
    text = (declared or "").upper()
    if "INT" in text:
        return "i"
    if "CHAR" in text or "CLOB" in text or "TEXT" in text:
        return "t"
    if "REAL" in text or "FLOA" in text or "DOUB" in text:
        return "r"
    if "BLOB" in text:
        return "b"
    return "t"


def get_table(catalog, table_name):
    table = next((t for t in catalog["tables"] if t["name"] == table_name), None)
    if table is None:  # 三份镜像模式一致且计划由该目录生成，正常不会发生。
        raise Problem("unknown_table", "计划引用的表在祖本目录中不存在。", 404,
                      {"table": table_name})
    return table


def assert_key_aligned(row, item, table, item_id=None):
    """主键只用于对齐：人工行解析出的键身份必须与计划条目完全一致。

    一致后把主键列规范化成条目键的 atom（整数/等值实数共享身份），避免
    1 与 1.0 这类同键异写在写后读比对里被当成不同存储类型。
    """
    columns = table["columns"]
    key_columns = table["key"]
    positions = [columns.index(key) for key in key_columns]
    submitted = [key_atom(unpack(row[pos])) for pos in positions]
    if submitted != item["key"]:
        info = {"stage": "decision_input", "step": "key_alignment", "item": item_id,
                "table": table["name"], "expected_key": item["key"], "submitted_key": submitted}
        raise Problem("manual_key",
                      "人工合成行的主键与裁决条目的对齐主键不一致；裁决不能修改主键，"
                      "请保持主键列不变或改裁对应键的条目。", 422, info)
    for key_index, position in enumerate(positions):
        row[position] = item["key"][key_index]


def _convert_list(raw, table, item_id):
    columns = table["columns"]
    if not isinstance(raw, list) or len(raw) != len(columns):
        raise Problem("manual_row",
                      f"人工行须是含 {len(columns)} 个单元格的数组，顺序对应表 "
                      f"{table['name']} 的列 {columns}。", 422,
                      {"stage": "decision_input", "step": "shape", "item": item_id,
                       "columns": columns})
    return [convert_cell(cell, column, pos, item_id)
            for pos, (column, cell) in enumerate(zip(columns, raw))]


def _base_or_nulls(base_row, count):
    if base_row is not None and len(base_row) == count:
        return list(base_row)
    return [["n"] for _ in range(count)]


def _convert_column_map(raw, table, base_row, item_id):
    columns = table["columns"]
    unknown = [key for key in raw if key not in columns]
    if unknown:
        raise Problem("manual_column", f"人工行包含表中不存在的列：{unknown}。", 422,
                      {"stage": "decision_input", "step": "shape", "item": item_id,
                       "unknown": unknown})
    row = _base_or_nulls(base_row, len(columns))
    for pos, column in enumerate(columns):
        if column in raw:
            row[pos] = convert_cell(raw[column], column, pos, item_id)
    return row


def _indexed_form(data, table, item_id):
    columns = table["columns"]
    types = table.get("types", [])
    row = [None] * len(columns)
    touched = False
    for pos, column in enumerate(columns):
        tag = _first(data, (f"mt:{pos}", f"mt_{pos}", f"type:{pos}", f"type_{pos}",
                            f"cell:{pos}:type"))
        text = _first(data, (f"mv:{pos}", f"mv_{pos}", f"value:{pos}", f"value_{pos}",
                             f"cell:{pos}:value"))
        if tag is None and text is None:
            continue
        touched = True
        declared = types[pos] if pos < len(types) else ""
        row[pos] = convert_typed_text(tag, text if text is not None else "",
                                      column, pos, item_id, declared)
    if not touched:
        return None
    if any(cell is None for cell in row):
        raise missing_row(item_id)
    return row


def _named_form(data, table, base_row, item_id):
    columns = table["columns"]
    values = {}
    touched = False
    for pos, column in enumerate(columns):
        text = None
        has_value = False
        tag = None
        for key in (column, f"value_{column}", f"val_{column}", f"v_{column}"):
            if key in data:
                text = data[key]
                has_value = True
                touched = True
                break
        for suffix in TYPE_SUFFIXES:
            if f"{column}{suffix}" in data:
                tag = data[f"{column}{suffix}"]
                break
        if tag is None:
            for prefix in TYPE_PREFIXES:
                if f"{prefix}{column}" in data:
                    tag = data[f"{prefix}{column}"]
                    break
        if tag is not None:
            touched = True
        if not has_value and tag is None:
            continue
        declared = table["types"][pos] if pos < len(table.get("types", [])) else ""
        if has_value and not isinstance(text, str):
            # JSON 原生值（null、数字、对象/数组标记）按其类型转换，
            # None 必须成为 NULL 而不是空文本。
            values[column] = convert_cell([tag, text] if tag else text,
                                          column, pos, item_id)
        else:
            values[column] = convert_typed_text(tag, text if isinstance(text, str) else "",
                                                column, pos, item_id, declared)
    if not touched:
        return None
    row = _base_or_nulls(base_row, len(columns))
    for pos, column in enumerate(columns):
        if column in values:
            row[pos] = values[column]
    return row


def _first(data, keys):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _raw_container(data):
    for field in ROW_FIELDS:
        if field in data and data[field] is not None:
            raw = data[field]
            if isinstance(raw, str):
                text = raw.strip()
                # 只有看起来像 JSON 容器的字符串才按行解析，避免把同名列的普通
                # 文本值误当成 row 字段。
                if not text or text[0] not in "[{":
                    continue
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise Problem("manual_row", f"字段 {field} 不是合法 JSON 行：{exc}", 422,
                                  {"stage": "decision_input", "step": "parse"}) from exc
            if isinstance(raw, (list, dict)):
                return raw
            # 标量可能恰好来自同名列的扁平表单，交给列名路径处理。
            continue
    return None


def build_manual_row(data, item, table, item_id=None):
    """从 API JSON 或网页表单字段构造并校验人工行 atom 数组。

    显式 ``row: null`` 表示用人工裁决删除对齐行；完全不带行字段才是请求形态错误。
    """
    item_id = item_id or item.get("id")
    explicit_null = any(field in data and data[field] is None for field in ROW_FIELDS) \
        if isinstance(data, dict) else False
    # 1) 本仓库网页使用的按列索引表单。
    row = _indexed_form(data, table, item_id) if isinstance(data, dict) else None
    if row is not None:
        assert_key_aligned(row, item, table, item_id)
        return row
    # 2) row/values/manual/... 容器（JSON 数组、列名映射或 JSON 字符串）。
    raw = _raw_container(data) if isinstance(data, dict) else None
    if raw is not None:
        if isinstance(raw, dict) and _is_envelope(raw):
            raw = raw.get("row", raw.get("values"))
        elif isinstance(raw, dict) and ("row" in raw or "values" in raw) and \
                not any(key in table["columns"] for key in raw):
            raw = raw.get("row", raw.get("values"))
        if raw is None:
            if explicit_null:
                return None
            raise missing_row(item_id)
        if isinstance(raw, dict):
            row = _convert_column_map(raw, table, item.get("base"), item_id)
        else:
            row = _convert_list(raw, table, item_id)
        assert_key_aligned(row, item, table, item_id)
        return row
    # 3) 直接以列名作为表单字段的兼容形态（缺列回退祖本值）。
    row = _named_form(data, table, item.get("base"), item_id) if isinstance(data, dict) else None
    if row is not None:
        assert_key_aligned(row, item, table, item_id)
        return row
    # 4) 显式 null：人工删除对齐行。
    if explicit_null:
        return None
    raise missing_row(item_id)


def _is_envelope(raw):
    return ("choice" in raw or "mode" in raw or "type" in raw) and \
        ("row" in raw or "values" in raw)


def normalize_key_ref(key):
    """把 item 对象引用里的键（可能用数字而非规范字符串）归一成条目键 atom。"""
    if not isinstance(key, (list, tuple)):
        return key
    result = []
    for part in key:
        atom = convert_cell(part)
        result.append(key_atom(unpack(atom)))
    return result


def decision_envelope(choice, data):
    """兼容 choice 直接写成 {"mode":"manual","row":...} 的请求。"""
    if isinstance(choice, dict):
        kind = choice.get("choice", choice.get("mode", choice.get("type", "manual")))
        kind = normalize_choice_name(kind) or kind
        merged = dict(data or {})
        for field in ROW_FIELDS:
            if field in choice and field not in merged:
                merged[field] = choice[field]
        return kind, merged
    return choice, data
