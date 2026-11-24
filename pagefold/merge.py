import json

from .codec import fingerprint


ROLES = ("base", "left", "right")

# 列级裁决状态
SAME = "same"            # 三方一致，无需处理
SIDE = "side"            # 仅一个分支改动，可以直接采纳
CONFLICT = "conflict"    # 同一列出现两个不同的新值，必须人工择一

# 整行合成的阻断原因
REASON_MISSING = "row_missing"   # 删除碰上修改，无法在缺席行上合成
REASON_CONFLICT = "column_conflict"  # 存在同列不同值，需要逐列裁决


def calculate(base, left, right):
    result = []
    for table in sorted(base):
        versions = [snapshot[table] for snapshot in (base, left, right)]
        keys = sorted(set().union(*versions))
        for key in keys:
            ancestor, lrow, rrow = (values.get(key) for values in versions)
            if lrow == rrow == ancestor:
                continue
            choice = "left" if lrow == rrow or rrow == ancestor else "right" if lrow == ancestor else None
            item_key = json.loads(key)
            result.append({"id": fingerprint([table, item_key]), "table": table, "key": item_key,
                           "base": ancestor, "left": lrow, "right": rrow, "automatic": choice})
    return result


def column_report(item, columns, key_columns):
    """逐列解释三方取值：状态、各角色取值、单边改动时的来源角色。"""
    report = []
    for index, name in enumerate(columns):
        if name in key_columns:
            continue
        values = {role: item[role][index] if item[role] is not None else None for role in ROLES}
        base_value, left_value, right_value = values["base"], values["left"], values["right"]
        if left_value == right_value:
            status = SAME if left_value == base_value else SIDE
            source = "left" if status == SIDE else None
        elif left_value == base_value:
            status, source = SIDE, "right"
        elif right_value == base_value:
            status, source = SIDE, "left"
        else:
            status, source = CONFLICT, None
        report.append({"name": name, "index": index, "status": status, "source": source, "values": values})
    return report


def merge_report(item, columns, key_columns):
    """返回该行是否可直接列合成、阻断原因、逐列报告以及建议的列映射。"""
    present = {role: item[role] is not None for role in ROLES}
    report = column_report(item, columns, key_columns) if all(present.values()) else []
    blockers = []
    suggested = {}
    if not all(present.values()):
        blockers.append(REASON_MISSING)
    conflicts = [column["name"] for column in report if column["status"] == CONFLICT]
    if conflicts:
        blockers.append(REASON_CONFLICT)
    if not blockers:
        suggested = {column["name"]: column["source"] for column in report if column["status"] == SIDE}
    return {"columns": report, "combinable": not blockers, "blockers": blockers,
            "conflict_columns": conflicts, "suggested": suggested,
            "present": present}


def synthesize_row(item, columns, key_columns, chosen):
    """按列映射合成目标行；键列固定取祖本/任一支的主键值。"""
    if any(item[role] is None for role in ROLES):
        return None
    positions = {name: index for index, name in enumerate(columns)}
    key_positions = {positions[name] for name in key_columns}
    row = list(item["base"])
    for name, role in chosen.items():
        if name not in positions or role not in ROLES:
            return None
        index = positions[name]
        if index in key_positions:
            return None
        row[index] = item[role][index]
    return row


def is_synthesis(decision):
    return isinstance(decision, dict) and decision.get("combine") is True


def selected(item, decisions):
    """返回 (角色标识, 目标行)。

    整行裁决的角色标识为 base/left/right；列合成时为 "combine"，
    目标行是保存裁决时已经固定下来的合成行，重演与页面都只读取这一份。
    """
    automatic = item["automatic"]
    if automatic:
        return automatic, item[automatic]
    decision = decisions.get(item["id"])
    if is_synthesis(decision):
        return "combine", decision["row"]
    if decision in ROLES:
        return decision, item[decision]
    return None, None
