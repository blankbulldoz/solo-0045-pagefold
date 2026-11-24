from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


CATEGORY_LABELS = {
    "general": "一般", "coverage": "覆盖", "override": "覆盖/优先",
    "overlap": "重叠", "exception": "例外",
}
SUGGESTION_STATUS = {
    "unanimous": ("规则一致", "auto"),
    "override_recommendation": ("高优先级覆盖", "auto"),
    "exception_recommendation": ("例外规则建议", "conflict"),
    "contested": ("重叠意见冲突", "conflict"),
    "manual_exception": ("例外·需人工", "conflict"),
    "no_match": ("未命中规则", "conflict"),
}


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


def attempts_list(attempts, revision):
    return "".join(fragment("attempt", revision=a["revision"],
                            status="通过" if a["status"] == "passed" else "受阻",
                            marker=" · 当前版" if revision == a["revision"] else " · 历史版",
                            evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
                            changes=a["changed"]) for a in attempts) or '<p class="muted">暂无重演记录。</p>'


def rule_opinion(hit):
    opinion = "交人工" if hit["choice"] == "manual" else ROLE_LABELS[hit["choice"]]
    reasons = "".join(f"<li>{safe(reason)}</li>" for reason in hit["reasons"]) or "<li>规则条件命中。</li>"
    draft = "" if hit.get("persisted", True) else '<span class="tag conflict">未保存草稿</span>'
    return f"""<li class="rule-opinion">
      <div class="rule-opinion-head">
        <b>{safe(hit['name'])}</b>
        <span class="tag">{CATEGORY_LABELS.get(hit['category'], hit['category'])} · P{hit['priority']}</span>
        {draft}
        <code class="rule-id">{safe(hit['rule_id'])}</code>
        <span class="opinion-choice">意见：<b>{safe(opinion)}</b></span>
      </div>
      <ul class="rule-reasons">{reasons}</ul>
    </li>"""


def suggestion_actions(plan, suggestion):
    if plan["state"] != "open":
        return '<p class="muted">计划已关闭，不能继续写入裁决。</p>'
    current = suggestion.get("current_choice")
    current_text = f'<p class="current-choice">当前裁决：<b>{ROLE_LABELS.get(current, "未裁决")}</b></p>'
    buttons = []
    recommended = suggestion.get("recommended_choice")
    for role, label in ROLE_LABELS.items():
        style = "primary" if role == recommended else "quiet-choice"
        text = f"采纳建议：{label}" if role == recommended else f"改选{label}"
        buttons.append(f'<button class="{style}" type="submit" name="choice" value="{role}">{text}</button>')
    return f"""{current_text}
    <form class="suggestion-apply" method="post" action="/plans/{plan['id']}/decisions">
      <input type="hidden" name="revision" value="{plan['revision']}">
      <input type="hidden" name="item" value="{suggestion['item_id']}">
      <div class="human-buttons">{''.join(buttons)}</div>
      <p class="muted">试算不会写入；只有点击按钮后才按当前版本保存裁决。</p>
    </form>"""


def suggestion_card(plan, suggestion):
    status_text, badge = SUGGESTION_STATUS.get(suggestion["status"], ("试算", "auto"))
    rec = suggestion.get("recommended_choice")
    rec_text = ROLE_LABELS.get(rec, "由人裁决")
    presence = "".join(
        f'<span class="presence {state}">{ROLE_LABELS[role]}：'
        f'{"整行缺席" if state == "absent" else "行存在"}</span>'
        for role, state in suggestion["presence"].items())
    changed = suggestion.get("changed_columns", {})
    changed_text = " · ".join(
        f"{ROLE_LABELS[role]}变更列：{('、'.join(cols) if cols else '无列级原地修改')}"
        for role, cols in changed.items())
    rules = "".join(rule_opinion(hit) for hit in suggestion["matched_rules"])
    if not rules:
        rules = '<li class="muted">没有规则命中。</li>'
    return f"""<article class="suggestion-card" data-item="{suggestion['item_id']}">
      <header class="suggestion-head">
        <div><h3>{safe(suggestion['table'])} <span>{values(suggestion['key'])}</span></h3>
        <p>{safe(suggestion['scenario']['description'])}</p></div>
        <span class="tag {badge}">{safe(status_text)} · 建议：{safe(rec_text)}</span>
      </header>
      <div class="presence-row">{presence}</div>
      <p class="muted changed-columns">{safe(changed_text)}</p>
      <p class="suggestion-explanation">{safe(suggestion['explanation'])}</p>
      <ul class="rule-opinions">{rules}</ul>
      {suggestion_actions(plan, suggestion)}
    </article>"""


def suggestions_panel(plan, trial):
    if trial is None:
        return '<p class="empty">规则试算暂不可用。</p>', "未试算"
    items = trial.get("suggestions", [])
    if not items:
        body = '<p class="empty">当前没有需要裁决的冲突，因此没有规则建议。</p>'
    else:
        body = "".join(suggestion_card(plan, item) for item in items)
    summary = trial["summary"]
    label = f"{summary['conflicts']} 项冲突 · {summary['recommended']} 项可采纳 · {summary['contested']} 项重叠冲突"
    return body, label


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def plan_page(plan, images, attempts, published, trial=None):
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
    suggestion_html, suggestion_summary = suggestions_panel(plan, trial)
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"]),
                suggestions=suggestion_html, suggestion_summary=safe(suggestion_summary),
                actions="".join(actions))
