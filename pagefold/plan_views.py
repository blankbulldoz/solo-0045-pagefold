from .blockers import KIND_LABELS
from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def entries(plan):
    blocks = []
    for item in sorted(plan["items"], key=lambda item: bool(item["automatic"])):
        form = ""
        auto = item["automatic"]
        if plan["state"] == "open" and auto is None:
            options = option("clear", "保留未决", chosen=item["choice"] is None)
            options += "".join(option(role, label, chosen=role == item["choice"])
                               for role, label in ROLE_LABELS.items())
            form = fragment("decision", plan=plan["id"], revision=plan["revision"], item=item["id"], options=options)
        choice = ROLE_LABELS.get(item["choice"], "待裁决")
        chosen = ("自动 · " if auto else "") + choice
        blocks.append(fragment("entry", table=safe(item["table"]), key=values(item["key"]),
                               choice=chosen, badge="auto" if auto else "conflict", form=form,
                               **{role: values(item[role]) for role in ROLE_LABELS}))
    return "".join(blocks) or '<div class="empty">三份镜像没有行差异。</div>'


def _row_identity(record):
    table = safe(record.get("table") or "?")
    key = values(record["key"]) if record.get("key") is not None else '<span class="null">未定位到行</span>'
    return f'<span class="bloc"><b>{table}</b> · {key}</span>'


def _chip(record):
    return f'<code class="bid">{safe(record["id"])}</code>'


def _verified_item(rec):
    kind = safe(KIND_LABELS.get(rec.get("kind"), rec.get("kind") or "约束"))
    constraint = f'<code>{safe(rec["constraint"])}</code>' if rec.get("constraint") else ""
    action = f'<small class="ba">处置：{safe(rec["action"])}</small>' if rec.get("action") else ""
    message = rec.get("message") or rec.get("detail") or ""
    return (f'<li>{_chip(rec)}<span class="bkind">{kind}</span>{_row_identity(rec)}{constraint}'
            f'<small>{safe(message)}</small>{action}</li>')


def _unchecked_item(rec):
    link = f'<small class="blink">被 {safe(rec["blocked_by"])} 拦截</small>' if rec.get("blocked_by") else ""
    fks = ""
    if rec.get("foreign_keys"):
        bits = []
        for fk in rec["foreign_keys"]:
            ref = f'{safe(fk["references"]["table"])}({safe(",".join(fk["references"]["columns"]))})'
            state = {"present": "目标父行在计划中存在", "absent": "目标父行缺失",
                     "parent_blocked": "父行同样未写入",
                     "nullable_skipped": "含 NULL，默认不强制"}.get(fk.get("target_state"), fk.get("target_state"))
            cols = safe(",".join(fk["columns"]))
            bits.append(f'<small class="bfk">外键 {cols} → {ref}：{safe(state)}</small>')
        fks = "".join(bits)
    return (f'<li>{_chip(rec)}{_row_identity(rec)}{link}'
            f'<small>{safe(rec.get("detail") or "")}</small>{fks}</li>')


def _gap_item(rec):
    link = f'<small class="blink">关联 {safe(rec["linked_to"])}</small>' if rec.get("linked_to") else ""
    action = f'<small class="ba">处置：{safe(rec["action"])}</small>' if rec.get("action") else ""
    return (f'<li>{_chip(rec)}{link}<small>{safe(rec.get("detail") or "")}</small>'
            f'<small class="ba">{safe(rec.get("affects") or "")}</small>{action}</li>')


def blocker_findings(blockers):
    if not blockers:
        return ""
    groups = (
        ("verified", "经过验证的原因", "bv", _verified_item),
        ("unchecked", "尚无法继续检查的条目", "bu", _unchecked_item),
        ("gaps", "仍会影响结论的缺口", "bg", _gap_item),
    )
    sections = []
    total = 0
    for name, title, cls, render_item in groups:
        records = blockers.get(name) or []
        total += len(records)
        if records:
            items = "".join(render_item(record) for record in records)
        else:
            items = '<li class="muted">无</li>'
        sections.append(
            f'<section class="bgroup {cls}"><h4>{title}<span class="tag">{len(records)}</span></h4>'
            f'<ul>{items}</ul></section>')
    return (f'<div class="blockers"><p class="blockers-title">阻断集合 · {total} 条标记</p>'
            f'{"".join(sections)}</div>')


def attempts_list(attempts, revision):
    rendered = []
    for a in attempts:
        findings = blocker_findings(a.get("blockers"))
        rendered.append(fragment("attempt", revision=a["revision"],
                                status="通过" if a["status"] == "passed" else "受阻",
                                marker=" · 当前版" if revision == a["revision"] else " · 历史版",
                                evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
                                changes=a["changed"], findings=findings))
    return "".join(rendered) or '<p class="muted">暂无重演记录。</p>'


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
