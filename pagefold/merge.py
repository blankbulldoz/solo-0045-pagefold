import json

from .codec import fingerprint


ROLES = ("base", "left", "right")
MANUAL = "manual"


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


def decision_name(stored):
    """裁决值可能是 "left" 这样的字符串，或 {"choice":"manual","row":[...]}。"""
    if isinstance(stored, dict):
        return stored.get("choice")
    return stored


def selected(item, decisions):
    stored = decisions.get(item["id"])
    role = item["automatic"] or decision_name(stored)
    if role is None:
        return None, None
    if role == MANUAL:
        # 已落库的人工裁决一定带规范 row；兼容直接存成 row 数组的形态。
        if isinstance(stored, dict):
            return MANUAL, stored.get("row")
        return MANUAL, stored
    return role, item[role] if role in item else None


def decision_entry(item_id, stored, items_by_id=None):
    """展开一条裁决为可写入凭证/计划 JSON 的追溯记录。"""
    name = decision_name(stored)
    entry = {"item": item_id, "choice": name, "digest": fingerprint(stored)}
    item = (items_by_id or {}).get(item_id)
    if item is not None:
        entry["table"] = item["table"]
        entry["key"] = item["key"]
        if name == MANUAL:
            _, entry["row"] = selected(item, {item_id: stored})
        else:
            entry["row"] = item.get(name) if name in ROLES else None
    return entry


def decision_manifest(items, decisions):
    by_id = {item["id"]: item for item in items}
    return [decision_entry(item_id, decisions[item_id], by_id)
            for item_id in sorted(decisions)]


def manual_rows_map(decisions):
    """{item_id: 人工行 atom 数组}，便于在计划/凭证里直接追溯人工选择。"""
    result = {}
    for item_id, stored in decisions.items():
        if decision_name(stored) == MANUAL and isinstance(stored, dict):
            result[item_id] = stored.get("row")
    return result
