from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def source_cards(images):
    content = [image["content_digest"] for image in images]
    cards = []
    for index, (label, image) in enumerate(zip(ROLE_LABELS.values(), images)):
        badges = []
        for other_index, other in enumerate(images):
            if other_index == index:
                continue
            if image["id"] == other["id"]:
                badges.append('<span class="tag" title="三个角色选用了同一个文件摘要">与另一角色为同一登记件</span>')
            elif image["content_digest"] and image["content_digest"] == content[other_index]:
                badges.append('<span class="tag auto" title="文件摘要不同，但模式与类型化内容完全一致，常见于 VACUUM 后重新登记">'
                              '碎片整理等价件</span>')
        badge_html = "".join(dict.fromkeys(badges))
        cards.append(
            f'<a href="/images/{image["id"]}"><small>{label}</small><b>{safe(image["label"])}</b>'
            f'<code>文件 {image["id"][:14]}</code>'
            f'<code class="content-id">内容 {safe((image["content_digest"] or "—")[:14])}</code>'
            f'{badge_html}</a>')
    return "".join(cards)


def identity_strip(pairs):
    if not pairs:
        return ""
    chips = []
    for label_a, label_b, id_a, id_b, report in pairs:
        verdict = report["verdict"]
        cls = "eq" if verdict == "defragmented_equivalent" else "shift" if verdict == "type_shifted_lookalike" else ""
        if verdict == "same_file":
            text = "同一文件摘要"
        elif verdict == "defragmented_equivalent":
            text = "碎片整理后等价"
        elif verdict == "type_shifted_lookalike":
            text = f'行位置相同但类型不同 · {report["type_change_count"]} 处'
        else:
            text = f'内容不同 · 类型差异 {report["type_change_count"]}'
        chips.append(
            f'<a class="identity-pair {cls}" href="/images/compare?a={id_a}&amp;b={id_b}">'
            f'<b>{label_a} ↔ {label_b}</b><span>{text}</span></a>')
    return ('<section class="paper identity-strip"><div class="panel-title"><h2>来源身份对照</h2>'
            '<span class="tag">文件摘要决定凭证来源</span></div>'
            f'<div class="identity-pairs">{"".join(chips)}</div></section>')


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


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def plan_page(plan, images, attempts, published, identity_html=""):
    actions = []
    if plan["state"] == "open":
        passed = any(a["revision"] == plan["revision"] and a["status"] == "passed" for a in attempts)
        actions.append(action_form(plan, "rehearse", "重演当前版", "primary", bool(plan["unresolved"])))
        actions.append(action_form(plan, "seal", "封存成果", disabled=not passed))
    if plan["state"] != "archived":
        actions.append(action_form(plan, "archive", "归档计划", "quiet"))
    if published:
        actions.append(fragment("release", plan=plan["id"]))
    roles = source_cards(images)
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                identity=identity_html,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions))
