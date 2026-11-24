"""显式结构桥接：把模式不同但语义对应的三份镜像映射到共同表和键空间。

操作者在新建计划时提交桥接声明：目标角色（共同模式取自哪一方）、共同表、
键空间，以及逐列的来源对应（改名来源或显式填充）。compute 校验声明并给出
可观察的桥接结果：可无损对应的部分生成行迁移影响报告，到不了同一目标的
部分列入 unreachable。translate 把各角色的行迁入共同空间并校验原身份，
migrate_ancestor 把祖本物理迁移到共同模式供原有重演流程使用。
"""

import re

from .catalog import describe
from .codec import atom, canonical, fingerprint, unpack
from .constraints import check_database
from .errors import Problem
from .merge import ROLES
from .rows import key_atom, read_image
from .sqlite_image import ident, mounted, validate_header

LOGICAL_FIELDS = ("columns", "types", "notnull", "defaults", "key", "foreign_keys",
                  "indexes", "without_rowid", "strict")


def declaration_problem(detail):
    return Problem("bridge_declaration", detail, 400)


def parse_default(text):
    """把目录记录的默认值 SQL 还原为字面值，只接受可安全重放的常量。"""
    value = text.strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if re.fullmatch(r"(?i)null", value):
        return None
    if re.fullmatch(r"(?i)true", value):
        return 1
    if re.fullmatch(r"(?i)false", value):
        return 0
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", value):
        return float(value)
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    blob = re.fullmatch(r"(?i)x'([0-9a-f]*)'", value)
    if blob and len(blob.group(1)) % 2 == 0:
        return bytes.fromhex(blob.group(1))
    raise ValueError(f"默认值 {text!r} 不是可重放的常量")


def _typed_default(text):
    try:
        return atom(parse_default(text))
    except ValueError:
        return None


def _validate(declaration):
    if not isinstance(declaration, dict):
        raise declaration_problem("bridge 须为对象，包含 target 和 tables。")
    if set(declaration) - {"target", "tables"}:
        raise declaration_problem("桥接声明只接受 target 和 tables 两个字段。")
    target = declaration.get("target")
    if target not in ROLES:
        raise declaration_problem("桥接声明的 target 须为 base、left 或 right。")
    specs = declaration.get("tables")
    if not isinstance(specs, list) or not specs:
        raise declaration_problem("桥接声明须列出至少一张共同表。")
    tables = []
    seen = set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {"name", "key", "from", "columns"}:
            raise declaration_problem("共同表只接受 name、key、from、columns 字段。")
        name = spec.get("name")
        if not isinstance(name, str) or not name or name in seen:
            raise declaration_problem("共同表的 name 须为不重复的非空文本。")
        seen.add(name)
        key = spec.get("key")
        if not isinstance(key, list) or not key or not all(isinstance(k, str) for k in key):
            raise declaration_problem(f"共同表 {name} 须声明非空的键空间 key。")
        sources = spec.get("from", {})
        if not isinstance(sources, dict) or not sources.keys() <= set(ROLES):
            raise declaration_problem(f"共同表 {name} 的 from 须按角色给出源表名。")
        if target in sources or not all(isinstance(v, str) and v for v in sources.values()):
            raise declaration_problem(f"共同表 {name} 的源表名无效或指向目标角色。")
        columns = spec.get("columns")
        if columns is not None:
            if not isinstance(columns, list) or not columns:
                raise declaration_problem(f"共同表 {name} 的 columns 须为非空列表。")
            for column in columns:
                if not isinstance(column, dict) or set(column) - {"name", "from"}:
                    raise declaration_problem(f"共同表 {name} 的列声明只接受 name、from 字段。")
                mapping = column.get("from", {})
                if not isinstance(mapping, dict) or not mapping.keys() <= set(ROLES):
                    raise declaration_problem(f"列 {column.get('name')} 的 from 须按角色给出源列名。")
                if target in mapping:
                    raise declaration_problem(f"列 {column.get('name')} 的来源不能是目标角色。")
                if not all(v is None or (isinstance(v, str) and v) for v in mapping.values()):
                    raise declaration_problem(f"列 {column.get('name')} 的源列名无效。")
        tables.append({"name": name, "key": key, "from": sources, "columns": columns})
    return target, tables


def _column_map(table):
    return {name: index for index, name in enumerate(table["columns"])}


def _normalize_type(text):
    return " ".join(str(text).split()).upper()


def _fk_footprint(table, columns, tables):
    result = set()
    for row in table["foreign_keys"]:
        _, _, ref, src, dst, on_update, on_delete, _ = row
        result.add((tables.get(ref, ref), columns.get(src, src),
                    columns.get(dst, dst) if dst is not None else None, on_update, on_delete))
    return result


def _index_footprint(table, columns):
    result = set()
    for index in table["indexes"]:
        mapped = tuple(columns.get(name, name) for name in index["columns"])
        marker = index["name"] if index["origin"] == "c" else ""
        result.add((marker, index["unique"], mapped))
    return result


def _logical_signature(tables):
    return fingerprint([{field: table[field] for field in ("name", *LOGICAL_FIELDS)}
                        for table in tables])


def compute(catalogs, declaration):
    """校验桥接声明，返回共同目录、翻译计划和可观察报告（含 unreachable）。"""
    target, specs = _validate(declaration)
    by_role = {role: {t["name"]: t for t in catalogs[role]["tables"]} for role in ROLES}
    unreachable = []

    def block(table, column, role, reason):
        unreachable.append({"table": table, "column": column, "role": role, "reason": reason})

    plans = {}
    common_tables = []
    for spec in specs:
        name = spec["name"]
        goal = by_role[target].get(name)
        if goal is None:
            raise declaration_problem(f"目标角色 {target} 中没有表 {name}。")
        if spec["key"] != goal["key"]:
            raise declaration_problem(f"共同表 {name} 的键空间声明与目标主键 {goal['key']} 不一致。")
        declared = spec["columns"] or [{"name": column} for column in goal["columns"]]
        names = [column["name"] for column in declared]
        if len(set(names)) != len(names):
            raise declaration_problem(f"共同表 {name} 的列声明存在重复。")
        unknown = set(names) - set(goal["columns"])
        if unknown:
            raise declaration_problem(f"共同表 {name} 声明了目标中不存在的列：{'、'.join(sorted(unknown))}。")
        for column in goal["columns"]:
            if column not in names:
                block(name, column, target, "目标列未纳入桥接声明")
        for column in spec["key"]:
            if column not in names:
                block(name, column, target, "键列未纳入桥接声明")
        sources = {role: spec["from"].get(role, name) for role in ROLES if role != target}
        plan = {"name": name, "key": list(spec["key"]), "columns": [c for c in goal["columns"] if c in names],
                "goal": goal, "spec": {c["name"]: c.get("from", {}) for c in declared},
                "sources": sources, "roles": {}, "broken": False}
        for role, source in sources.items():
            origin = by_role[role].get(source)
            if origin is None:
                raise declaration_problem(f"角色 {role} 中没有声明的源表 {source}。")
            plan["roles"][role] = {"table": source, "origin": origin}
        plans[name] = plan
        keep = [goal["columns"].index(column) for column in plan["columns"]]
        common_tables.append({"name": name, "columns": plan["columns"],
                              "types": [goal["types"][i] for i in keep],
                              "notnull": [goal["notnull"][i] for i in keep],
                              "defaults": [goal["defaults"][i] for i in keep],
                              "key": list(spec["key"]), "foreign_keys": goal["foreign_keys"],
                              "indexes": goal["indexes"], "without_rowid": goal["without_rowid"],
                              "strict": goal["strict"]})

    used = {role: set() for role in ROLES}
    for plan in plans.values():
        used[target].add(plan["name"])
        for role, side in plan["roles"].items():
            used[role].add(side["table"])
    for role in ROLES:
        for table in by_role[role]:
            if table not in used[role]:
                block(table, None, role, "表未纳入桥接声明")

    for plan in plans.values():
        name, goal = plan["name"], plan["goal"]
        goal_index = _column_map(goal)
        goal_key = set(plan["key"])
        for role, side in plan["roles"].items():
            origin = side["origin"]
            mapping, fills = {}, {}
            if (origin["without_rowid"], origin["strict"]) != (goal["without_rowid"], goal["strict"]):
                block(name, None, role, "表的物理结构（WITHOUT ROWID / STRICT）与目标不一致")
                plan["broken"] = True
                continue
            for column in plan["columns"]:
                gpos = goal_index[column]
                source = plan["spec"][column].get(role, column)
                if source is None:
                    if column in goal_key:
                        block(name, column, role, "键列不能由填充产生")
                        plan["broken"] = True
                        continue
                    if goal["defaults"][gpos] is not None:
                        value = _typed_default(goal["defaults"][gpos])
                        if value is None:
                            block(name, column, role, "目标列的默认值不是可重放的常量")
                            plan["broken"] = True
                            continue
                    elif goal["notnull"][gpos]:
                        block(name, column, role, "角色缺席该列，且目标列 NOT NULL 无默认值")
                        plan["broken"] = True
                        continue
                    else:
                        value = ["n"]
                    mapping[column], fills[column] = None, value
                    continue
                if source not in origin["columns"]:
                    raise declaration_problem(f"角色 {role} 的表 {side['table']} 中没有声明的源列 {source}。")
                if source in mapping.values():
                    raise declaration_problem(f"角色 {role} 的源列 {source} 被重复映射。")
                opos = _column_map(origin)[source]
                checks = [(_normalize_type(origin["types"][opos]) == _normalize_type(goal["types"][gpos]),
                           "类型不一致")]
                if role == "base":
                    # 祖本列随物理迁移携带自身定义，须与目标列完全一致；
                    # 其他角色只有值迁入共同列，类型一致即可无损对应。
                    checks += [(origin["notnull"][opos] == goal["notnull"][gpos], "空值约束不一致"),
                               ((origin["defaults"][opos] or "").strip()
                                == (goal["defaults"][gpos] or "").strip(), "默认值不一致")]
                for ok, reason in checks:
                    if not ok:
                        block(name, column, role, f"列属性与目标{reason}")
                        plan["broken"] = True
                mapping[column] = source
            consumed = {src for src in mapping.values() if src is not None}
            key_sources = {mapping[k] for k in plan["key"] if mapping.get(k) is not None}
            if len(key_sources) == len(plan["key"]) and key_sources != set(origin["key"]):
                block(name, None, role, "映射后的键空间与该角色的主键不一致")
                plan["broken"] = True
            for column in origin["columns"]:
                if column not in consumed:
                    block(name, column, role, "列未纳入桥接声明")
                    plan["broken"] = True
            side["map"], side["fills"] = mapping, fills

    for plan in plans.values():
        name, goal = plan["name"], plan["goal"]
        common_fk = _fk_footprint(goal, {}, {})
        common_ix = _index_footprint(goal, {})
        for role, side in plan["roles"].items():
            if plan["broken"]:
                continue
            mapping = side["map"]
            reverse = {src: dst for dst, src in mapping.items() if src is not None}
            tables = {side2["table"]: plan2["name"] for plan2 in plans.values()
                      for side2 in [plan2["roles"].get(role)] if side2}
            origin = side["origin"]
            if any(ix["partial"] or ix["expression"] for ix in origin["indexes"] + goal["indexes"]):
                block(name, None, role, "部分索引或表达式索引暂不支持桥接")
                plan["broken"] = True
                continue
            if _fk_footprint(origin, reverse, tables) != common_fk:
                block(name, None, role, "外键关系映射后与目标不一致")
                plan["broken"] = True
            if _index_footprint(origin, reverse) != common_ix:
                block(name, None, role, "索引映射后与目标不一致")
                plan["broken"] = True

    catalog = {"signature": _logical_signature(common_tables), "tables": common_tables}
    report = {"target": target, "schema": catalog["signature"], "tables": [], "identity": {},
              "alterations": [], "unreachable": unreachable}
    return {"target": target, "catalog": catalog, "plans": plans, "report": report}


def _key_token(row, positions):
    return canonical([key_atom(unpack(row[pos])) for pos in positions])


def translate(result, role, rows):
    """把一个角色的行迁入共同空间，校验原身份并记录迁移影响。"""
    output = {}
    identity = {}
    for plan in result["plans"].values():
        name = plan["name"]
        if plan["broken"]:
            continue
        side = plan["roles"].get(role)
        source = side["table"] if side else name
        mapping = side["map"] if side else {column: column for column in plan["columns"]}
        fills = side["fills"] if side else {}
        origin = side["origin"] if side else plan["goal"]
        positions = [origin["columns"].index(mapping[column]) if mapping[column] is not None else None
                     for column in plan["columns"]]
        key_positions = [plan["columns"].index(column) for column in plan["key"]]
        mapped = [pos for pos in positions if pos is not None]
        impact = {"source": source, "rows": 0, "keys_remapped": 0,
                  "renamed": {mapping[c]: c for c in plan["columns"]
                              if mapping[c] is not None and mapping[c] != c},
                  "filled": {c: fills[c] for c in plan["columns"] if c in fills},
                  "reordered": mapped != sorted(mapped)}
        moved = {}
        for token, row in rows.get(source, {}).items():
            merged = [row[pos] if pos is not None else fills[column]
                      for pos, column in zip(positions, plan["columns"])]
            token2 = _key_token(merged, key_positions)
            if token2 in moved:
                raise Problem("bridge_identity",
                              f"表 {name} 在角色 {role} 中的行身份桥接后冲突。", 409)
            impact["keys_remapped"] += token2 != token
            moved[token2] = merged
        impact["rows"] = len(moved)
        identity[name] = {"rows_in": len(rows.get(source, {})), "rows_out": len(moved),
                          "keys_remapped": impact["keys_remapped"]}
        result["report"]["tables"].append({"table": name, "role": role, **impact})
        output[name] = moved
    result["report"]["identity"][role] = identity
    return output


def _column_ddl(goal, gpos):
    parts = [goal["types"][gpos]] if goal["types"][gpos] else []
    if goal["notnull"][gpos]:
        parts.append("NOT NULL")
    if goal["defaults"][gpos] is not None:
        parts.append(f"DEFAULT {goal['defaults'][gpos]}")
    return " ".join(parts)


def migrate_ancestor(body, result):
    """把祖本镜像物理迁移到共同模式，返回 (镜像字节, 执行的 ALTER 列表)。"""
    alterations = []
    for plan in result["plans"].values():
        side = plan["roles"].get("base")
        if side is None:
            continue
        goal = plan["goal"]
        if side["table"] != plan["name"]:
            alterations.append(f"ALTER TABLE {ident(side['table'])} RENAME TO {ident(plan['name'])}")
        for column in plan["columns"]:
            source = side["map"][column]
            if source is None:
                ddl = _column_ddl(goal, goal["columns"].index(column))
                alterations.append(f"ALTER TABLE {ident(plan['name'])} "
                                   f"ADD COLUMN {ident(column)} {ddl}".rstrip())
            elif source != column:
                alterations.append(f"ALTER TABLE {ident(plan['name'])} "
                                   f"RENAME COLUMN {ident(source)} TO {ident(column)}")
    if not alterations:
        return body, []
    with mounted(body, writable=True) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for statement in alterations:
            db.execute(statement)
        migrated = describe(db)
        if len(migrated["tables"]) != len(result["catalog"]["tables"]):
            raise Problem("bridge_migration", "迁移后的祖本表集合与共同模式不一致。", 500)
        common = {t["name"]: t for t in result["catalog"]["tables"]}
        for table in migrated["tables"]:
            goal = common.get(table["name"])
            if goal is None:
                raise Problem("bridge_migration", f"迁移后出现计划外表 {table['name']}。", 500)
            same_shape = (
                dict(zip(table["columns"], table["types"])) == dict(zip(goal["columns"], goal["types"]))
                and dict(zip(table["columns"], table["notnull"])) == dict(zip(goal["columns"], goal["notnull"]))
                and dict(zip(table["columns"], table["defaults"])) == dict(zip(goal["columns"], goal["defaults"]))
                and sorted(table["key"]) == sorted(goal["key"])
                and table["without_rowid"] == goal["without_rowid"] and table["strict"] == goal["strict"]
                and _fk_footprint(table, {}, {}) == _fk_footprint(goal, {}, {})
                and _index_footprint(table, {}) == _index_footprint(goal, {}))
            if not same_shape:
                raise Problem("bridge_migration", f"迁移后的表 {table['name']} 与共同模式不一致。", 500)
        check_database(db)
        db.execute("COMMIT")
        data = db.serialize()
    validate_header(data)
    return data, alterations
