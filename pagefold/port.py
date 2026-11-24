"""分支变更移植引擎。

把"上轮祖本 → 改动分支"之间的修改移植到新祖本。复用 merge.calculate 的三方
按键真值，再叠加两层移植语义：

1. 每个条目给出明确处置（自动移植 / 已提前到位 / 保留新祖本 / 重新选择 / 待决）
   与来源（改动分支或新祖本），逐行可交代。
2. 复合主键重命名在分支库里表现为"删旧键 + 插新键"两个条目，这里按非键负载把
   两半识别为同一改名组；组的完整性单独汇总为完整移植 / 保留未改名 / 部分移植 /
   待裁决 / 受阻，绝不把半套动作报成完整移植。
"""

from .codec import fingerprint
from .merge import calculate, selected

# base=上轮祖本，left=改动分支（移植来源），right=新祖本（移植目标）
PORT_ROLES = ("base", "left", "right")
PORT_ROLE_LABELS = {"base": "上轮祖本", "left": "改动分支", "right": "新祖本"}

DISPOSITION_LABELS = {
    "auto": "自动移植",
    "already": "已提前到位",
    "keep": "保留新祖本",
    "reselect": "重新选择",
    "pending": "待裁决",
}

# 改名组在给定裁决下的完成度
GROUP_COMPLETE = "complete"      # 删旧与插新两半都落地：完整移植了改名
GROUP_RETAINED = "retained"      # 两半都选择保留：新祖本维持旧实体，改名未移植
GROUP_PARTIAL = "partial"        # 只落下一半，属于不完整的半套动作
GROUP_PENDING = "pending"        # 组内还有未裁决冲突
GROUP_BLOCKED = "blocked"        # 重演时外键等约束拦截，改名无法完成

GROUP_LABELS = {
    GROUP_COMPLETE: "完整移植",
    GROUP_RETAINED: "保留未改名",
    GROUP_PARTIAL: "部分移植",
    GROUP_PENDING: "待裁决",
    GROUP_BLOCKED: "受阻未完成",
}


def _payload(catalog, table, row):
    """取一行的非主键负载，用于识别同一实体的主键改名。"""
    if row is None:
        return None
    table_obj = next(t for t in catalog["tables"] if t["name"] == table)
    positions = [table_obj["columns"].index(col) for col in table_obj["key"]]
    return [value for index, value in enumerate(row) if index not in positions]


def build_groups(items, catalog):
    """把同表的"分支删旧 + 分支插新"且非键负载一致的两个条目配成改名组。

    其余条目标记为独立改动（group 为 None）。返回 (group_map, order)。
    """
    grouped = set()
    groups = {}
    order = []
    by_table = {}
    for item in items:
        by_table.setdefault(item["table"], []).append(item)
    for table, entries in by_table.items():
        removed = [i for i in entries if i["base"] is not None and i["left"] is None]
        inserted = [i for i in entries if i["base"] is None and i["left"] is not None]
        for old in removed:
            old_payload = _payload(catalog, table, old["base"])
            for new in inserted:
                if new["id"] in grouped:
                    continue
                if _payload(catalog, table, new["left"]) == old_payload:
                    group_id = fingerprint(["rename", table, old["id"], new["id"]])
                    groups[group_id] = {
                        "id": group_id, "table": table, "kind": "rename",
                        "old_item": old["id"], "new_item": new["id"],
                        "old_key": old["key"], "new_key": new["key"],
                    }
                    order.append(group_id)
                    grouped.update((old["id"], new["id"]))
                    break
    group_map = {}
    for group in groups.values():
        group_map[group["old_item"]] = group["id"]
        group_map[group["new_item"]] = group["id"]
    return group_map, order, groups


def _disposition(item, decisions):
    """根据三方真值和当前裁决，给出条目处置与来源。

    - 自动项选改动分支：目标已同值记"已提前到位"，否则记"自动移植"。
    - 自动项选新祖本：单边改动在目标侧，记"自动移植"（保留目标值）。
    - 冲突项由人工裁决到新祖本：保留；到改动分支/上轮祖本：重新选择。
    """
    automatic = item["automatic"]
    if automatic is not None:
        if automatic == "left":
            if item["right"] == item["left"]:
                return "already", "left"
            return "auto", "left"
        return "auto", "right"
    choice = decisions.get(item["id"])
    if choice == "right":
        return "keep", "right"
    if choice in ("left", "base"):
        return "reselect", choice
    return "pending", None


def calculate_port(snapshots, catalog, decisions=None):
    """计算移植计划。snapshots 顺序为 (上轮祖本, 改动分支, 新祖本)。"""
    decisions = decisions or {}
    items = calculate(*snapshots)
    group_map, order, groups = build_groups(items, catalog)
    detailed = []
    for item in items:
        disposition, source = _disposition(item, decisions)
        role, selected_row = selected(item, decisions)
        detailed.append({
            **item,
            "group": group_map.get(item["id"]),
            "disposition": disposition,
            "source": source,
            "choice": role,
            "selected": selected_row,
        })
    rename_groups = [summarize_group(groups[gid], detailed) for gid in order]
    return {"items": detailed, "groups": rename_groups}


def summarize_group(group, detailed):
    """汇总改名组两半的处置，明确区分完整移植、保留未改名与部分移植。"""
    members = {item["id"]: item for item in detailed}
    old = members[group["old_item"]]
    new = members[group["new_item"]]
    half_states = []
    pending = False
    for member, half in ((old, "delete_old"), (new, "insert_new")):
        if member["disposition"] == "pending":
            pending = True
        # 该半动作在目标库的落地结果：选择的行是否存在
        lands = member["selected"] is not None
        half_states.append({"half": half, "item": member["id"], "key": member["key"],
                            "disposition": member["disposition"], "lands": lands})
    delete_lands, insert_lands = (h["lands"] for h in half_states)
    if pending:
        status = GROUP_PENDING
    elif not delete_lands and insert_lands:
        # 旧键被删、新键存在：删旧 + 插新两半齐备，完整移植改名
        status = GROUP_COMPLETE
    elif delete_lands and not insert_lands:
        # 旧键保留、新键反而缺席：只完成删除侧的相反半套，仍是部分移植
        status = GROUP_PARTIAL
    else:
        # 旧键与新键都保留在目标库：未采纳改名，保留新祖本现状
        status = GROUP_RETAINED
    return {**group, "status": status, "halves": half_states}


def mark_blocked_groups(port_view, problem_code):
    """重演受阻后，把待落地的改名组标记为 blocked，避免误报完整移植。

    待决组保持 pending；明确保留（retained）的组不依赖本次重演，不改状态。
    """
    blocked = set()
    for group in port_view["groups"]:
        if group["status"] in (GROUP_PENDING, GROUP_RETAINED):
            continue
        group["status"] = GROUP_BLOCKED
        group["blocked_by"] = problem_code
        blocked.add(group["id"])
    for item in port_view["items"]:
        if item["group"] in blocked:
            item["blocked"] = True
    return port_view


def unresolved_count(items, decisions):
    return sum(selected(item, decisions)[0] is None for item in items)
