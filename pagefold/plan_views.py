import json

from .codec import fingerprint
from .render import (CHOICE_LABELS, ROLE_LABELS, STATE_LABELS, TYPE_OPTIONS, atom_input_value,
                     fragment, option, page, safe, values)


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def _type_options(current):
    return "".join(f'<option value="{tag}"{" selected" if tag == current else ""}>{safe(label)}</option>'
                   for tag, label in TYPE_OPTIONS)


def _editor_cell(item, table, pos):
    column = table["columns"][pos]
    declared = table.get("types", [""] * len(table["columns"]))[pos]
    key_columns = table["key"]
    is_key = column in key_columns
    if is_key:
        atom = item["key"][key_columns.index(column)]
    elif item.get("choice") == "manual" and item.get("selected_row") is not None:
        selected_row = item["selected_row"]
        atom = selected_row[pos] if pos < len(selected_row) else ["n"]
    else:
        base_row = item.get("base")
        atom = base_row[pos] if base_row is not None and pos < len(base_row) else ["n"]
    tag, value = atom_input_value(atom)
    lock = '<em class="key-mark" title="主键用于对齐，裁决不能修改它">主键 · 对齐锁定</em>' if is_key else ""
    readonly = " readonly aria-readonly=\"true\"" if is_key else ""
    return (
        f'<label class="cell{" key-cell" if is_key else ""}" data-column="{safe(column)}">'
        f'<span class="col-head"><b>{safe(column)}</b>{lock}<i>{safe(declared or "无类型")}</i></span>'
        f'<select name="mt:{pos}" class="cell-type" aria-label="{safe(column)} 类型">{_type_options(tag)}</select>'
        f'<input class="cell-value" name="mv:{pos}" value="{safe(value)}"{readonly} autocomplete="off" '
        f'placeholder="NULL 选 n；文本留空且类型为 t 即空文本"></label>'
    )


def manual_editor(item, table):
    cells = "".join(_editor_cell(item, table, pos) for pos in range(len(table["columns"])))
    return (
        '<div class="manual-editor">'
        '<p class="manual-hint">人工合成行：默认带入祖本值，可逐列改类型和内容。'
        '主键列只用于对齐，提交不同主键会被拒绝；<code>n</code> 是 NULL，'
        '类型 <code>t</code> 且内容为空是空文本；<code>b</code> 填 base64。</p>'
        f'<div class="manual-grid">{cells}</div></div>'
    )


def manual_summary(item, table):
    row = item.get("selected_row")
    digest = fingerprint({"choice": "manual", "row": row})
    if row is None:
        return (f'<div class="manual-selection" data-item="{item["id"]}" data-digest="{digest}">'
                f'<span class="manual-stamp">人工裁决：删除/保持缺席 <code>{digest[:16]}</code></span></div>')
    columns = table["columns"] if table else [f"列 {i + 1}" for i in range(len(row))]
    details = "".join(
        f'<div class="manual-cell"><dt>{safe(columns[pos] if pos < len(columns) else pos)}</dt>'
        f'<dd>{values([atom])}</dd></div>'
        for pos, atom in enumerate(row))
    return (
        f'<div class="manual-selection" data-item="{item["id"]}" data-digest="{digest}">'
        f'<span class="manual-stamp">人工合成已采纳 <code>{digest[:16]}</code></span>'
        f'<dl class="manual-detail">{details}</dl></div>'
    )


def decision_form(plan, item, table):
    options = option("clear", "保留未决", chosen=item["choice"] is None)
    options += "".join(option(role, label, chosen=role == item["choice"])
                       for role, label in ROLE_LABELS.items())
    options += option("manual", "人工合成一行…", chosen=item["choice"] == "manual")
    current_manual = item.get("selected_row") if item.get("choice") == "manual" else None
    rowsjson = safe(json.dumps({
        "base": item.get("base"), "left": item.get("left"), "right": item.get("right"),
        "manual": current_manual,
    }, ensure_ascii=False))
    return fragment("decision", plan=plan["id"], revision=plan["revision"], item=item["id"],
                    options=options, editor=manual_editor(item, table), rowsjson=rowsjson,
                    manualclass="manual-on" if item["choice"] == "manual" else "")


def entries(plan, tables):
    blocks = []
    for item in sorted(plan["items"], key=lambda entry: bool(entry["automatic"])):
        form = ""
        manual = ""
        auto = item["automatic"]
        table = tables.get(item["table"])
        if item["choice"] == "manual" and table is not None:
            manual = manual_summary(item, table)
        if plan["state"] == "open" and auto is None and table is not None:
            form = decision_form(plan, item, table)
        choice = CHOICE_LABELS.get(item["choice"], "待裁决")
        chosen = ("自动 · " if auto else "") + choice
        blocks.append(fragment("entry", itemid=item["id"], table=safe(item["table"]),
                               key=values(item["key"]), choice=chosen,
                               badge="auto" if auto else "conflict", manual=manual, form=form,
                               **{role: values(item[role]) for role in ROLE_LABELS}))
    return "".join(blocks) or '<div class="empty">三份镜像没有行差异。</div>'


def attempts_list(attempts, revision):
    blocks = []
    for attempt in attempts:
        if attempt["status"] == "passed":
            evidence = safe(attempt.get("candidate_digest") or "")
        else:
            problem = attempt.get("problem") or {}
            stage = "输入转换" if problem.get("stage") == "decision_input" else "重演/约束校验"
            evidence = safe(f"[{problem.get('code', 'error')}]（{stage}步骤未被接受）{problem.get('detail', '')}")
        blocks.append(fragment("attempt", revision=attempt["revision"],
                                status="通过" if attempt["status"] == "passed" else "受阻",
                                marker=" · 当前版" if revision == attempt["revision"] else " · 历史版",
                                evidence=evidence, changes=attempt["changed"]))
    return "".join(blocks) or '<p class="muted">暂无重演记录。</p>'


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def plan_page(plan, images, attempts, published):
    actions = []
    if plan["state"] == "open":
        passed = any(a["revision"] == plan["revision"] and a["status"] == "passed" for a in attempts)
        actions.append(action_form(plan, "rehearse", "重演当前版", "primary", bool(plan["unresolved"])))
        actions.append(action_form(plan, "seal", "封存成果", disabled=not passed))
    if plan["state"] != "archived":
        actions.append(action_form(plan, "archive", "归档计划", "quiet"))
    if published:
        actions.append(fragment("release", plan=plan["id"]))
    base_image = next((image for image in images if image["id"] == plan["sources"]["base"]), images[0])
    tables = {table["name"]: table for table in json.loads(base_image["catalog"])["tables"]}
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>' for label, i in zip(ROLE_LABELS.values(), images))
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan, tables),
                history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions))
