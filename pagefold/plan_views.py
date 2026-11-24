from .merge import CONFLICT, REASON_CONFLICT, REASON_MISSING, SIDE
from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, typed, values

BLOCKER_LABELS = {
    REASON_MISSING: "删除与修改相遇：整行在某一支已缺席，无法按列合成，请整行裁决。",
    REASON_CONFLICT: "存在同列不同值：下列高亮列须逐列选定来源后才能合成。",
}
STATUS_LABELS = {"same": "三方一致", "side": "单边改动", "conflict": "同列冲突"}
STATUS_CLASSES = {"same": "col-same", "side": "col-side", "conflict": "col-conflict"}


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def role_cell(atom):
    return typed(atom) if atom is not None else '<span class="null">缺席</span>'


def column_grid(merge):
    cells = []
    for column in merge["columns"]:
        source = ROLE_LABELS[column["source"]] if column["source"] else "—"
        values_by_role = column["values"]
        cells.append(
            f'<div class="column-cell {STATUS_CLASSES[column["status"]]}">'
            f'<dt><b>{safe(column["name"])}</b><small>{STATUS_LABELS[column["status"]]} · 建议来源 {source}</small></dt>'
            f'<dd><span>祖本</span>{role_cell(values_by_role["base"])}'
            f'<span>左支</span>{role_cell(values_by_role["left"])}'
            f'<span>右支</span>{role_cell(values_by_role["right"])}</dd></div>')
    return "".join(cells)


def blocker_notes(merge):
    if not merge["blockers"]:
        return ""
    notes = "".join(f"<li>{safe(BLOCKER_LABELS[code])}</li>" for code in merge["blockers"])
    return f'<ul class="blockers">{notes}</ul>'


def merge_panel(item):
    merge = item["merge"]
    synthesis = item["synthesis"]
    present = all(merge["present"].values())
    if not present:
        state, badge, badge_text = "missing", "conflict", "不可合成"
    elif synthesis is not None:
        state, badge, badge_text = "adopted", "adopted", "已经采纳 · 列合成"
    elif merge["combinable"]:
        state, badge, badge_text = "available", "combine", "可以组合"
    else:
        state, badge, badge_text = "manual", "conflict", "逐列裁决后可组合"
    result = ""
    if synthesis is not None:
        sources = "，".join(f"{safe(name)} ← {ROLE_LABELS[role]}" for name, role in synthesis["columns"].items())
        result = (f'<div class="synthesis-result"><h5>合成结果（重演目标）</h5>'
                  f'<p class="synthesis-sources">{sources or "全部列沿用祖本"}</p>'
                  f'<p class="synthesis-row">{values(synthesis["row"])}</p></div>')
    elif merge["combinable"]:
        sources = "，".join(
            f'{safe(column["name"])} ← {ROLE_LABELS[column["source"]]}'
            for column in merge["columns"] if column["status"] == SIDE)
        result = (f'<div class="synthesis-proposal"><h5>系统建议</h5>'
                  f'<p class="synthesis-sources">{sources or "无需改动列"}，'
                  f'选择“列合成（采纳建议）”即可一次采纳双方独立改动。</p></div>')
    return fragment("merge", state=state, badge=badge, badge_text=badge_text,
                    blockers=blocker_notes(merge), columns=column_grid(merge), result=result)


def column_picker(item):
    merge = item["merge"]
    synthesis = item["synthesis"]
    chosen = synthesis["columns"] if synthesis else {}
    fields = []
    for column in merge["columns"]:
        options = option("", "请选择来源")
        for role, label in ROLE_LABELS.items():
            picked = chosen.get(column["name"]) == role
            if picked is False and column["status"] != CONFLICT and column["source"] == role:
                picked = True  # 未采纳时预选系统建议，操作者仍可改选
            options += option(role, label, chosen=picked)
        fields.append(
            f'<label class="col-choice">{safe(column["name"])}'
            f'<select name="col.{safe(column["name"])}">{options}</select></label>')
    return "".join(fields)


def decision_form(plan, item):
    mode = "combine" if item["synthesis"] is not None else (item["choice"] or "clear")
    options = option("clear", "保留未决", chosen=mode == "clear")
    for role, label in ROLE_LABELS.items():
        options += option(role, f"整行裁决 · {label}", chosen=mode == role)
    if all(item["merge"]["present"].values()):
        options += option("combine", "列合成（逐列选择）", chosen=mode == "combine")
    form = fragment("decision", plan=plan["id"], revision=plan["revision"], item=item["id"],
                    options=options, column_picker=column_picker(item))
    return form


def entries(plan):
    blocks = []
    for item in sorted(plan["items"], key=lambda item: bool(item["automatic"])):
        auto = item["automatic"]
        if auto:
            form, merge = "", ""
        else:
            form = decision_form(plan, item) if plan["state"] == "open" else ""
            merge = merge_panel(item)
        choice = ROLE_LABELS.get(item["choice"], "列合成" if item["choice"] == "combine" else "待裁决")
        chosen = ("自动 · " if auto else "") + choice
        blocks.append(fragment("entry", table=safe(item["table"]), key=values(item["key"]),
                               choice=chosen, badge="auto" if auto else "conflict", form=form, merge=merge,
                               **{role: values(item[role]) for role in ROLE_LABELS}))
    return "".join(blocks) or '<div class="empty">三份镜像没有行差异。</div>'


def attempts_list(attempts, revision):
    return "".join(fragment("attempt", revision=a["revision"],
                            status="通过" if a["status"] == "passed" else "受阻",
                            marker=" · 当前版" if revision == a["revision"] else " · 历史版",
                            evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
                            changes=a["changed"]) for a in attempts) or '<p class="muted">暂无重演记录。</p>'


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
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>' for label, i in zip(ROLE_LABELS.values(), images))
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions))
