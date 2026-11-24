import json
import uuid

from .errors import Problem
from .merge import ROLES, selected


ROLE_LABELS = {"base": "祖本", "left": "左支", "right": "右支"}
RULE_CHOICES = (*ROLES, "manual")
PRIORITY_MIN = -10_000
PRIORITY_MAX = 10_000


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_rule(data, rule_id=None):
    """Validate and normalize a declarative row-selection rule."""
    if not isinstance(data, dict):
        raise Problem("invalid_rule", "规则必须是 JSON 对象。", 400)
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise Problem("invalid_rule", "规则名称须为 1 至 100 个字符。", 400)

    choice = data.get("choice", data.get("recommendation", data.get("opinion")))
    if choice not in RULE_CHOICES:
        raise Problem("invalid_rule", "规则意见须为 base、left、right 或 manual。", 400)

    priority = data.get("priority", 100)
    if not _is_int(priority) or not PRIORITY_MIN <= priority <= PRIORITY_MAX:
        raise Problem("invalid_rule", f"priority 须为 {PRIORITY_MIN} 到 {PRIORITY_MAX} 的整数。", 400)

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise Problem("invalid_rule", "enabled 须为布尔值。", 400)

    tables = data.get("tables", [])
    if isinstance(tables, str):
        tables = [tables]
    if not isinstance(tables, list) or any(not isinstance(t, str) or not t for t in tables):
        raise Problem("invalid_rule", "tables 须为表名字符串或字符串数组。", 400)

    when = data.get("when", data.get("match"))
    if when is None:
        if isinstance(data.get("conditions"), list):
            when = {"op": "all", "conditions": data["conditions"]}
        else:
            when = {"op": "all", "conditions": []}
    when = normalize_condition(when)
    validate_condition(when)

    description = data.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str) or len(description) > 500:
        raise Problem("invalid_rule", "规则说明最长 500 个字符。", 400)

    category = data.get("category", "general")
    if category not in {"general", "coverage", "override", "overlap", "exception"}:
        raise Problem("invalid_rule", "未知规则类别。", 400)

    if rule_id is None:
        candidate = data.get("id")
        if candidate is not None:
            if not isinstance(candidate, str) or not candidate or len(candidate) > 64:
                raise Problem("invalid_rule", "规则 id 须为最长 64 字符的字符串。", 400)
            rule_id = candidate
        else:
            rule_id = uuid.uuid4().hex
    return {
        "id": rule_id,
        "name": name.strip(),
        "description": description,
        "category": category,
        "enabled": enabled,
        "priority": priority,
        "choice": choice,
        "tables": tables,
        "when": when,
    }


def normalize_condition(node):
    if not isinstance(node, dict):
        return node
    if "op" in node:
        return node
    if "all" in node:
        return {"op": "all", "conditions": node["all"]}
    if "any" in node:
        return {"op": "any", "conditions": node["any"]}
    if "none" in node:
        return {"op": "not", "condition": {"op": "any", "conditions": node["none"]}}
    return node


def validate_condition(node, depth=0):
    if depth > 12:
        raise Problem("invalid_rule", "规则条件嵌套不能超过 12 层。", 400)
    node = normalize_condition(node)
    if not isinstance(node, dict) or not isinstance(node.get("op"), str):
        raise Problem("invalid_rule", "每个条件须包含 op。", 400)
    op = node["op"]
    if op in {"all", "any"}:
        conditions = node.get("conditions")
        if not isinstance(conditions, list):
            raise Problem("invalid_rule", f"{op} 条件须包含 conditions 数组。", 400)
        for child in conditions:
            validate_condition(child, depth + 1)
        return
    if op == "not":
        validate_condition(node.get("condition"), depth + 1)
        return

    roles = ("base", "left", "right")
    if op == "row_state":
        _require_role(node, roles)
        if node.get("state") not in {"present", "absent"}:
            raise Problem("invalid_rule", "row_state.state 须为 present 或 absent。", 400)
    elif op == "change_kind":
        _require_role(node, roles)
        kinds = {"unchanged", "created", "updated", "deleted"}
        if node.get("kind") not in kinds and not (
            isinstance(node.get("kinds"), list) and all(k in kinds for k in node["kinds"])
        ):
            raise Problem("invalid_rule", "change_kind 须提供有效 kind 或 kinds。", 400)
    elif op == "branch_changed":
        _require_role(node, roles)
        if "kinds" in node and (not isinstance(node["kinds"], list) or
                                any(k not in {"created", "updated", "deleted"} for k in node["kinds"])):
            raise Problem("invalid_rule", "branch_changed.kinds 包含无效变更类型。", 400)
    elif op in {"column_state", "column_changed"}:
        _require_role(node, roles)
        if not isinstance(node.get("column"), str) or not node["column"]:
            raise Problem("invalid_rule", f"{op} 须提供 column 列名。", 400)
        if op == "column_state" and node.get("state") not in {
            "present", "absent", "row_present", "row_absent", "sql_null", "not_null"
        }:
            raise Problem("invalid_rule", "column_state.state 含义无效。", 400)
    elif op == "only_changed_columns":
        _require_role(node, roles)
        columns = node.get("columns")
        if not isinstance(columns, list) or not columns or any(not isinstance(c, str) for c in columns):
            raise Problem("invalid_rule", "only_changed_columns 须提供非空 columns 字符串数组。", 400)
    elif op == "delete_vs_edit":
        if node.get("deleted_role") not in {"left", "right"}:
            raise Problem("invalid_rule", "delete_vs_edit.deleted_role 须为 left 或 right。", 400)
        if node.get("edited_role") not in {"left", "right"} or node["edited_role"] == node["deleted_role"]:
            raise Problem("invalid_rule", "delete_vs_edit.edited_role 须为另一支。", 400)
    else:
        raise Problem("invalid_rule", f"不支持的条件 op：{op}", 400)


def _require_role(node, roles):
    if node.get("role") not in roles:
        raise Problem("invalid_rule", "条件 role 须为 base、left 或 right。", 400)


def create_rule(store, data):
    rule = normalize_rule(data)
    with store.connect(write=True) as db:
        if db.execute("SELECT 1 FROM rules WHERE id=?", (rule["id"],)).fetchone():
            raise Problem("rule_exists", "相同 id 的规则已存在。", 409)
        db.execute("INSERT INTO rules(id,name,spec,builtin) VALUES(?,?,?,?)",
                   (rule["id"], rule["name"], json.dumps(rule, ensure_ascii=True, sort_keys=True), 0))
        return get_rule(db, rule["id"])


def get_rule(db, rule_id):
    row = db.execute("SELECT id,name,spec,builtin FROM rules WHERE id=?", (rule_id,)).fetchone()
    if row is None:
        raise Problem("unknown_rule", "找不到指定规则。", 404)
    result = json.loads(row["spec"])
    result["builtin"] = bool(row["builtin"])
    return result


def list_rules(db):
    result = []
    for row in db.execute("SELECT id,name,spec,builtin FROM rules ORDER BY rowid ASC"):
        rule = json.loads(row["spec"])
        rule["builtin"] = bool(row["builtin"])
        result.append(rule)
    return result


def rule_record(rule, builtin=False):
    return dict(rule, builtin=builtin)


def build_context(item, columns_by_table):
    columns = columns_by_table.get(item["table"], [])
    kinds = {}
    changed = {}
    changes = {}
    for role in ROLES:
        base = item.get("base")
        row = item.get(role)
        if base is None and row is None:
            kind = "unchanged"
        elif base is None:
            kind = "created"
        elif row is None:
            kind = "deleted"
        elif base == row:
            kind = "unchanged"
        else:
            kind = "updated"
        kinds[role] = kind
        if kind == "updated":
            different = [i for i in range(min(len(base), len(row))) if base[i] != row[i]]
            changed[role] = [columns[i] if i < len(columns) else f"#{i + 1}" for i in different]
            changes[role] = {
                changed[role][index]: {"from": base[i], "to": row[i]}
                for index, i in enumerate(different)
            }
        else:
            changed[role] = None
            changes[role] = None
    return {"item": item, "columns": columns, "kinds": kinds,
            "changed_columns": changed, "column_changes": changes}


def _cell(ctx, role, column):
    item = ctx["item"]
    row = item.get(role)
    if row is None:
        return None, "absent", None
    columns = ctx["columns"]
    if column not in columns:
        return None, "unknown", None
    index = columns.index(column)
    if index >= len(row):
        return None, "unknown", None
    atom_value = row[index]
    return atom_value, "sql_null" if atom_value == ["n"] else "value", index


def _atom_text(value):
    if value is None:
        return "缺席"
    if value == ["n"]:
        return "SQL NULL"
    if not isinstance(value, list) or not value:
        return json.dumps(value, ensure_ascii=False)
    kind, *rest = value
    text = rest[0] if rest else ""
    labels = {"i": "整数", "r": "实数", "t": "文本", "b": "BLOB"}
    shown = "<空 BLOB>" if kind == "b" and not text else text
    return f"{labels.get(kind, kind)} {shown}"


def evaluate_condition(node, ctx):
    node = normalize_condition(node)
    op = node["op"]
    item = ctx["item"]

    if op == "all":
        results = [evaluate_condition(c, ctx) for c in node.get("conditions", [])]
        ok = all(r["matched"] for r in results)
        return {"op": op, "matched": ok, "summary": "全部条件成立" if ok else "存在不成立条件", "children": results}
    if op == "any":
        results = [evaluate_condition(c, ctx) for c in node.get("conditions", [])]
        ok = any(r["matched"] for r in results)
        return {"op": op, "matched": ok, "summary": "任一条件成立" if ok else "没有条件成立", "children": results}
    if op == "not":
        child = evaluate_condition(node["condition"], ctx)
        return {"op": "not", "matched": not child["matched"],
                "summary": "否定条件成立" if not child["matched"] else "否定条件不成立", "child": child}

    role = node.get("role")
    label = ROLE_LABELS.get(role, "")
    if op == "row_state":
        state = node["state"]
        absent = item.get(role) is None
        if state == "absent":
            ok = absent
            summary = f"{label}整行缺席：该主键不存在；这不是列中的 SQL NULL"
        else:
            ok = not absent
            summary = f"{label}整行存在；即使某列为 SQL NULL，行也不算缺席"
        return {"op": op, "matched": ok, "summary": summary, "evidence": {"role": role, "state": "absent" if absent else "present"}}

    if op == "change_kind":
        wanted = node.get("kinds") or [node.get("kind")]
        actual = ctx["kinds"][role]
        ok = actual in wanted
        return {"op": op, "matched": ok,
                "summary": f"{label}相对祖本为 {actual}" + ("" if ok else f"，不是 {'/'.join(wanted)}"),
                "evidence": {"role": role, "actual": actual, "wanted": wanted}}

    if op == "branch_changed":
        actual = ctx["kinds"][role]
        allowed = set(node.get("kinds", ["created", "updated", "deleted"]))
        ok = actual in allowed
        detail = {"role": role, "actual": actual}
        if actual == "deleted":
            summary = f"{label}删除了同键行（整行缺席）"
        elif actual == "created":
            summary = f"{label}新增了该键行"
        elif actual == "updated":
            cols = ctx["changed_columns"][role]
            summary = f"{label}改写了同键行，变化列：{', '.join(cols) if cols else '未知'}"
            detail["changed_columns"] = cols
        else:
            summary = f"{label}相对祖本未变"
        return {"op": op, "matched": ok, "summary": summary, "evidence": detail}

    if op == "column_state":
        column = node["column"]
        cell, cell_state, _ = _cell(ctx, role, column)
        wanted = node["state"]
        row_absent = item.get(role) is None
        if wanted in {"absent", "row_absent"}:
            ok = row_absent
            summary = f"{label}整行缺席，所以列 {column} 不存在" if ok else f"{label}行存在；列缺席条件不成立"
        elif wanted in {"present", "row_present"}:
            ok = not row_absent and cell_state != "unknown"
            summary = f"{label}列 {column} 存在" if ok else f"{label}列 {column} 无法读取或整行缺席"
        elif wanted == "sql_null":
            ok = cell_state == "sql_null"
            if row_absent:
                summary = f"{label}整行缺席；缺席不等于列 {column} 的 SQL NULL"
            else:
                summary = f"{label}列 {column} 是 SQL NULL" if ok else f"{label}列 {column} 不是 SQL NULL"
        else:  # not_null
            ok = cell_state == "value"
            if row_absent:
                summary = f"{label}整行缺席；无法把缺席判定为 {column} NOT NULL"
            elif cell_state == "sql_null":
                summary = f"{label}列 {column} 是 SQL NULL，不满足 NOT NULL"
            else:
                summary = f"{label}列 {column} 为非空值 {_atom_text(cell)}"
        return {"op": op, "matched": ok, "summary": summary,
                "evidence": {"role": role, "column": column, "cell_state": cell_state}}

    if op == "column_changed":
        column = node["column"]
        kind = ctx["kinds"][role]
        if kind != "updated":
            return {"op": op, "matched": False,
                    "summary": f"{label}行为 {kind}；整行新增/删除不能当作列 {column} 的原地修改",
                    "evidence": {"role": role, "kind": kind, "column": column}}
        changes = ctx["column_changes"][role] or {}
        ok = column in changes
        if ok:
            change = changes[column]
            summary = f"{label}修改列 {column}：{_atom_text(change['from'])} → {_atom_text(change['to'])}"
        else:
            summary = f"{label}没有修改列 {column}"
        return {"op": op, "matched": ok, "summary": summary, "evidence": (changes or {}).get(column)}

    if op == "only_changed_columns":
        wanted = set(node["columns"])
        kind = ctx["kinds"][role]
        actual_set = set(ctx["changed_columns"][role] or [])
        if kind != "updated":
            return {"op": op, "matched": False,
                    "summary": f"{label}行为 {kind}；无法用“仅修改列”描述整行新增/删除",
                    "evidence": {"role": role, "kind": kind}}
        ok = actual_set == wanted
        return {"op": op, "matched": ok,
                "summary": f"{label}实际仅修改 {sorted(actual_set)}" + ("" if ok else f"，规则要求仅修改 {sorted(wanted)}"),
                "evidence": {"actual": sorted(actual_set), "wanted": sorted(wanted)}}

    if op == "delete_vs_edit":
        deleted = node["deleted_role"]
        edited = node["edited_role"]
        deleted_ok = ctx["kinds"][deleted] == "deleted"
        edited_ok = ctx["kinds"][edited] == "updated"
        ok = deleted_ok and edited_ok
        cols = ctx["changed_columns"][edited] or []
        summary = (f"{ROLE_LABELS[deleted]}删除同键导致整行缺席，"
                   f"{ROLE_LABELS[edited]}改写同键（变化列：{', '.join(cols) if cols else '无'}）")
        return {"op": op, "matched": ok, "summary": summary,
                "evidence": {"deleted_role": deleted, "edited_role": edited,
                             "deleted_kind": ctx["kinds"][deleted], "edited_kind": ctx["kinds"][edited],
                             "edited_columns": cols}}

    raise Problem("invalid_rule", f"不支持的条件 op：{op}", 400)


def evaluate_rule(rule, item, ctx):
    tables = rule.get("tables") or []
    table_ok = not tables or item["table"] in tables
    trace = {"op": "all", "matched": table_ok, "summary": "表范围匹配" if table_ok else "表范围不匹配",
             "children": []}
    reasons = []
    if table_ok:
        result = evaluate_condition(rule.get("when", {"op": "all", "conditions": []}), ctx)
        trace["children"] = [result]
        matched = bool(result["matched"])
        _collect_reasons(result, reasons)
        if matched and not reasons:
            reasons.append("条件为空或仅含空 all：在指定表范围内覆盖全部冲突行")
    else:
        matched = False
        reasons.append(f"规则仅适用于表 {tables}，当前表为 {item['table']}")
    return {
        "rule_id": rule["id"], "name": rule["name"], "description": rule.get("description", ""),
        "category": rule.get("category", "general"), "priority": rule["priority"],
        "choice": rule["choice"], "matched": matched,
        "persisted": rule.get("persisted", True),
        "reasons": reasons if matched else [],
        "trace": trace,
    }


def _collect_reasons(result, out):
    if result.get("matched") and result.get("summary") and result.get("op") not in {"all", "any", "not"}:
        out.append(result["summary"])
    for child in result.get("children", []):
        _collect_reasons(child, out)
    if "child" in result:
        _collect_reasons(result["child"], out)


def classify_conflict(ctx):
    item = ctx["item"]
    kinds = ctx["kinds"]
    tags = []
    descriptions = []
    for deleted, edited in (("left", "right"), ("right", "left")):
        if kinds[deleted] == "deleted" and kinds[edited] == "updated":
            tag = f"{deleted}_delete_{edited}_edit"
            tags.append(tag)
            descriptions.append(f"{ROLE_LABELS[deleted]}删除、{ROLE_LABELS[edited]}修改同键")
    if kinds["left"] == "updated" and kinds["right"] == "updated":
        tags.append("parallel_updates")
        descriptions.append("两支都改写了同键行")
    for role in ("left", "right"):
        cols = ctx["changed_columns"][role] or []
        if "weight" in cols:
            tags.append(f"{role}_changed_weight")
            descriptions.append(f"{ROLE_LABELS[role]}修改了 weight")
        if kinds[role] == "updated" and set(cols) == {"weight"}:
            tags.append(f"only_{role}_weight_changed")
            descriptions.append(f"只有{ROLE_LABELS[role]}修改 weight 列")
    null_roles = []
    for role in ("left", "right"):
        cell, state, _ = _cell(ctx, role, "weight")
        if state == "sql_null":
            null_roles.append(role)
            tags.append(f"{role}_weight_sql_null")
            descriptions.append(f"{ROLE_LABELS[role]}存在的行中 weight 为 SQL NULL")
    if item.get("left") is None or item.get("right") is None:
        tags.append("row_absent_present")
        descriptions.append("至少一支是整行缺席，而非 SQL NULL 单元格")
    return {"tags": tags, "description": "；".join(dict.fromkeys(descriptions)) or "其他行分歧"}


def trial_items(items, columns_by_table, rules, decisions=None):
    decisions = decisions or {}
    suggestions = []
    for item in items:
        if item.get("automatic") is not None:
            continue
        ctx = build_context(item, columns_by_table)
        evaluations = [evaluate_rule(rule, item, ctx) for rule in rules if rule.get("enabled", True)]
        matched = [e for e in evaluations if e["matched"]]
        matched.sort(key=lambda e: (-e["priority"], e["rule_id"]))
        all_choices = {e["choice"] for e in matched}
        top_priority = matched[0]["priority"] if matched else None
        top = [e for e in matched if e["priority"] == top_priority]
        top_choices = {e["choice"] for e in top}
        recommended = None
        status = "no_match"
        explanation = "没有启用规则命中，保留人工裁决。"
        if matched:
            if len(top_choices) == 1:
                recommended = next(iter(top_choices))
                if recommended == "manual":
                    recommended = None
                    status = "manual_exception"
                    explanation = "最高优先级规则要求人工判断，不自动给出行选择。"
                elif any(rule.get("category") == "exception" for rule in top):
                    status = "exception_recommendation"
                    explanation = f"命中高优先级例外规则，建议 {recommended}；一般规则在该条件下不适用，但是否采纳仍由人决定。"
                elif len(all_choices) > 1:
                    status = "override_recommendation"
                    lower = [e for e in matched if e["priority"] != top_priority and e["choice"] != recommended]
                    explanation = f"最高优先级规则给出 {recommended}；它覆盖了 {len(lower)} 条不同意见。"
                else:
                    status = "unanimous"
                    explanation = "所有命中规则意见一致。"
            else:
                status = "contested"
                choices = "、".join(sorted(top_choices))
                explanation = f"相同最高优先级的规则给出不同意见（{choices}），试算不指定结论，须由人选择。"
        current_role, _ = selected(item, decisions)
        suggestions.append({
            "item_id": item["id"], "table": item["table"], "key": item["key"],
            "current_choice": current_role,
            "scenario": classify_conflict(ctx),
            "presence": {role: ("absent" if item.get(role) is None else "present") for role in ROLES},
            "change_kinds": ctx["kinds"],
            "changed_columns": ctx["changed_columns"],
            "status": status,
            "explanation": explanation,
            "recommended_choice": recommended,
            "opinions_differ": len(all_choices) > 1,
            "matched_rules": matched,
            "rule_evaluations": evaluations,
        })
    counts = {"conflicts": len(suggestions), "recommended": 0, "contested": 0,
              "exception_recommendation": 0, "manual_exception": 0, "no_match": 0, "decided": 0}
    for suggestion in suggestions:
        if suggestion["recommended_choice"]:
            counts["recommended"] += 1
        counts[suggestion["status"]] = counts.get(suggestion["status"], 0) + 1
        if suggestion["current_choice"]:
            counts["decided"] += 1
    return {"dry_run": True, "writes_decisions": False, "summary": counts, "suggestions": suggestions}
