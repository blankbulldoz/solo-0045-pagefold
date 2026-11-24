"""关系集合合流的范围分析。

范围内表的三方差异是主动变更；范围外表默认逐行保持祖本。本模块从目录中的外键与
唯一约束推导出维持关系完整性所必需的范围外派生删除，以及当前范围无法闭环的阻塞。
"""

import json
import math

from .codec import canonical, fingerprint
from .merge import selected


def fk_groups(table):
    grouped = {}
    for row in table["foreign_keys"]:
        fk_id, seq, parent, from_col, to_col = row[0], row[1], row[2], row[3], row[4]
        grouped.setdefault(fk_id, {"parent": parent, "pairs": []})
        grouped[fk_id]["pairs"].append((seq, from_col, to_col))
    return [{**group, "pairs": [pair[1:] for pair in sorted(group["pairs"])]}
            for group in grouped.values()]


def references(catalog):
    """父表 -> 引用它的 (子表, 列对) 列表。"""
    result = {}
    for child in catalog["tables"]:
        for group in fk_groups(child):
            result.setdefault(group["parent"], []).append((child["name"], group["pairs"]))
    return result


def identity_atom(atom_value):
    """行身份归一：整数与精确等值实数同身份，文本/BLOB 各自区分（与 rows.key_atom 对齐）。"""
    kind, payload = atom_value
    if kind == "r":
        number = float.fromhex(payload)
        if math.isfinite(number) and number.is_integer() and -(2**63) <= number < 2**63:
            return ["i", str(int(number))]
    return atom_value


def key_of_row(row, meta):
    positions = [meta["columns"].index(key) for key in meta["key"]]
    return [row[pos] for pos in positions]


def planned_snapshot(catalog, base_snapshot, items, decisions, scoped):
    planned = {table["name"]: dict(base_snapshot[table["name"]]) for table in catalog["tables"]}
    for item in items:
        if item["table"] not in scoped:
            continue
        role, target = selected(item, decisions)
        if role is None or target == item["base"]:
            continue
        token = canonical(item["key"])
        if target is None:
            planned[item["table"]].pop(token, None)
        else:
            planned[item["table"]][token] = target
    return planned


def blocker(kind, table, key, detail, action, tables):
    identity = fingerprint([kind, table, key, action, tables])
    return {"id": identity, "kind": kind, "table": table, "key": key, "detail": detail,
            "remedy": {"action": action, "tables": tables}}


def analyze(catalog, base_snapshot, items, decisions, scope, confirmed):
    metas = {table["name"]: table for table in catalog["tables"]}
    scoped = set(scope)
    planned = planned_snapshot(catalog, base_snapshot, items, decisions, scoped)
    refs = references(catalog)
    removed = {name: set() for name in metas}
    queue = []
    for name in scope:
        for token in base_snapshot[name]:
            if token not in planned[name]:
                removed[name].add(token)
                queue.append((name, token))

    derived = {}
    blockers = {}

    def add_blocker(entry):
        blockers[entry["id"]] = entry

    while queue:
        parent_name, parent_token = queue.pop(0)
        parent_meta = metas[parent_name]
        parent_row = base_snapshot[parent_name][parent_token]
        for child_name, pairs in refs.get(parent_name, []):
            child_meta = metas[child_name]
            child_pos = {col: child_meta["columns"].index(col) for col, _ in pairs}
            parent_positions = [parent_meta["columns"].index(to_col) for _, to_col in pairs]
            wanted = tuple(identity_atom(parent_row[pos]) for pos in parent_positions)
            for token, row in list(planned[child_name].items()):
                actual = tuple(identity_atom(row[child_pos[from_col]]) for from_col, _ in pairs)
                if any(value[0] == "n" for value in actual) or actual != wanted:
                    continue
                if child_name in scoped:
                    add_blocker(blocker(
                        "child_in_scope", child_name, key_of_row(row, child_meta),
                        f"范围内子表 {child_name} 仍引用被移除的 {parent_name} 父行",
                        "narrow", [parent_name]))
                    continue
                if token in removed[child_name]:
                    continue
                entry_id = fingerprint(["derived", child_name, json.loads(token), "delete"])
                derived[entry_id] = {"id": entry_id, "table": child_name,
                                     "key": json.loads(token), "row": row,
                                     "reason": f"外键 {child_name} → {parent_name} 失去父行，跟随删除"}
                planned[child_name].pop(token)
                removed[child_name].add(token)
                queue.append((child_name, token))

    for name in scope:
        meta = metas[name]
        positions = {col: meta["columns"].index(col) for col in meta["columns"]}
        for group in fk_groups(meta):
            parent_name = group["parent"]
            parent_meta = metas[parent_name]
            parent_index = {
                tuple(canonical(identity_atom(row[parent_meta["columns"].index(col)]))
                      for col in parent_meta["key"])
                for row in planned[parent_name].values()}
            for token, row in planned[name].items():
                foreign = tuple(canonical(identity_atom(row[positions[from_col]]))
                                for from_col, _ in group["pairs"])
                if any(value[0] == "n" for value in foreign):
                    continue
                if foreign not in parent_index:
                    detail = f"{name} 行引用的 {parent_name} 父行在目标态缺席"
                    if parent_name in scoped:
                        add_blocker(blocker("orphan_parent", name, json.loads(token), detail,
                                            "narrow", [name]))
                    else:
                        add_blocker(blocker("orphan_parent", name, json.loads(token), detail,
                                            "expand", [parent_name]))

    for name in scope:
        meta = metas[name]
        positions = {col: meta["columns"].index(col) for col in meta["columns"]}

        def unique_values(row, unique):
            return tuple(canonical(identity_atom(row[positions[col]])) for col in unique)

        for unique in meta.get("unique_keys", []):
            has_null = lambda row: any(
                identity_atom(row[positions[col]])[0] == "n" for col in unique)
            planned_index = {}
            for token, row in planned[name].items():
                if has_null(row):
                    continue
                planned_index.setdefault(unique_values(row, unique), []).append(token)
            for values, holders in planned_index.items():
                if len(holders) > 1:
                    add_blocker(blocker(
                        "unique_collision", name, json.loads(holders[-1]),
                        f"唯一约束 {tuple(unique)} 与另一条目标行冲突", "narrow", [name]))
            base_values = {token: unique_values(row, unique)
                           for token, row in base_snapshot[name].items() if not has_null(row)}
            untouched_values = {value for token, value in base_values.items()
                                if token in planned[name]
                                and base_values[token] == unique_values(planned[name][token], unique)}
            for token in base_values:
                if token not in planned[name]:
                    continue
                target = planned[name][token]
                if has_null(target) or base_values[token] == unique_values(target, unique):
                    continue
                if unique_values(target, unique) in untouched_values:
                    add_blocker(blocker(
                        "unique_collision", name, json.loads(token),
                        f"唯一约束 {tuple(unique)} 与祖本未改行冲突", "narrow", [name]))

    active = []
    active_changes = 0
    for item in items:
        if item["table"] not in scoped:
            continue
        role, target = selected(item, decisions)
        will_change = role is not None and target != item["base"]
        active_changes += int(will_change)
        active.append({"id": item["id"], "table": item["table"], "key": item["key"],
                       "choice": role, "will_change": will_change})

    derived_list = sorted(derived.values(), key=lambda entry: (entry["table"], canonical(entry["key"])))
    confirmed_set = set(confirmed)
    return {
        "scope": list(scope),
        "untouched_tables": [name for name in metas if not removed[name]],
        "active": active,
        "active_change_count": active_changes,
        "derived": derived_list,
        "confirmed": [entry["id"] for entry in derived_list if entry["id"] in confirmed_set],
        "pending_derived": [entry["id"] for entry in derived_list if entry["id"] not in confirmed_set],
        "blocked": sorted(blockers.values(), key=lambda entry: (entry["table"], entry["id"])),
    }


def operations(report, items, decisions):
    """把范围报告还原为 replay 需要的 {table: [(item, row), ...]}。"""
    result = {}
    chosen = {entry["id"]: entry for entry in report["active"] if entry["will_change"]}
    for item in items:
        if item["id"] in chosen:
            _, target = selected(item, decisions)
            result.setdefault(item["table"], []).append((item, target))
    by_id = {entry["id"]: entry for entry in report["derived"]}
    for entry_id in report["confirmed"]:
        entry = by_id[entry_id]
        item = {"table": entry["table"], "key": entry["key"], "base": entry["row"]}
        result.setdefault(entry["table"], []).append((item, None))
    return result
