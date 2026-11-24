import json

from .codec import canonical
from .errors import Problem
from .images import get_image
from .merge import ROLES, is_synthesis, merge_report, synthesize_row
from .plans import expect_open, expect_revision, get_plan, plan_content


def key_positions(catalog, table_name):
    table = next(table for table in catalog["tables"] if table["name"] == table_name)
    return table["columns"], table["key"]


def normalized_columns(raw):
    """JSON 直接提交 {列名: 角色}；网页表单经 API 转成同样的映射后进入这里。"""
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError as exc:
            raise Problem("column_choice", "列合成的列映射无法解析。", 400) from exc
    if not isinstance(raw, dict):
        raise Problem("column_choice", "列合成须提供列名到角色的映射。", 400)
    columns = {}
    for name, role in raw.items():
        if not isinstance(name, str) or role not in ROLES:
            raise Problem("column_choice", "列合成只能为每列选择祖本、左支或右支。", 400)
        columns[name] = role
    return columns


def build_synthesis(item, catalog, chosen):
    columns, key_columns = key_positions(catalog, item["table"])
    unknown = set(chosen) - (set(columns) - set(key_columns))
    if unknown:
        raise Problem("unknown_column",
                      f"表 {item['table']} 没有可裁决列：{'、'.join(sorted(unknown))}。", 400)
    if any(item[role] is None for role in ROLES):
        raise Problem("row_missing", "该分支已删除整行，删除与修改相遇只能整行裁决，不能列合成。", 409)
    # 同列不同值必须由操作者显式选择，不允许沿用建议外的默认值。
    report = merge_report(item, columns, key_columns)
    missing_choice = [name for name in report["conflict_columns"] if name not in chosen]
    if missing_choice:
        raise Problem("column_conflict",
                      f"列 {'、'.join(missing_choice)} 在两个分支各有不同的新值，须逐列选定来源。", 409)
    row = synthesize_row(item, columns, key_columns, chosen)
    if row is None:
        raise Problem("column_choice", "列合成的选择无法组成有效行。", 400)
    return {"combine": True, "columns": chosen, "row": row}


def resolve(store, plan_id, revision, item_id, choice, columns_raw=None):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        items, decisions = plan_content(plan)
        item = next((item for item in items if item["id"] == item_id and item["automatic"] is None), None)
        if item is None:
            raise Problem("not_conflict", "该条目不是可裁决的冲突。", 409)
        if choice == "clear":
            new_choice = None
        elif choice in ROLES:
            new_choice = choice
        elif choice == "combine":
            catalog = json.loads(get_image(db, plan["base"])["catalog"])
            new_choice = build_synthesis(item, catalog, normalized_columns(columns_raw))
        else:
            raise Problem("choice", "请选择祖本、左支、右支、列合成或清除裁决。", 400)
        if new_choice != decisions.get(item_id):
            if new_choice is None:
                decisions.pop(item_id)
            else:
                decisions[item_id] = new_choice
            revision += 1
            db.execute("UPDATE plans SET decisions=?,revision=? WHERE id=?",
                       (canonical(decisions), revision, plan_id))
        return {"id": plan_id, "revision": revision, "item": item_id,
                "choice": "combine" if is_synthesis(new_choice) else new_choice}
