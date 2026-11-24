from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def entries(plan):
    blocks = []
    report = plan.get("scope_report")
    scoped = set(report["scope"]) if report else None
    for item in sorted(plan["items"], key=lambda item: bool(item["automatic"])):
        if scoped is not None and item["table"] not in scoped:
            continue
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
    return "".join(blocks) or '<div class="empty">该关系集合内没有行差异。</div>'


def scope_panel(plan):
    report = plan["scope_report"]
    locked = "disabled" if plan["state"] != "open" else ""
    active_rows = "".join(
        f'<li><code>{safe(a["table"])}</code> {values(a["key"])} '
        f'<span class="tag {"auto" if a["will_change"] else ""}">{safe(a["choice"] or "待裁决")}</span></li>'
        for a in report["active"]) or '<li class="muted">无主动变更。</li>'
    confirmed = set(report["confirmed"])
    derived_rows = "".join(
        f'<li><code>{safe(d["table"])}</code> {values(d["key"])} '
        f'<span class="tag {"auto" if d["id"] in confirmed else "conflict"}">'
        f'{"已确认" if d["id"] in confirmed else "待确认"}</span><small>{safe(d["reason"])}</small></li>'
        for d in report["derived"]) or '<li class="muted">无需范围外派生变更。</li>'
    blocked_rows = "".join(
        f'<li class="blocker"><code>{safe(b["table"])}</code> {values(b["key"])} '
        f'<span class="tag conflict">{safe(b["remedy"]["action"])}</span>'
        f'<small>{safe(b["detail"])}；建议表：{safe("、".join(b["remedy"]["tables"]))}</small></li>'
        for b in report["blocked"]) or '<li class="muted">无阻塞，关系集合可在确认派生变更后重演。</li>'
    return fragment(
        "scope", plan=plan["id"], revision=plan["revision"],
        scope_mode=("全表合流" if report["full_schema"] else "受限关系集合"),
        scope_tables=safe("、".join(report["scope"])),
        untouched=safe("、".join(report["untouched_tables"]) or "（无）"),
        scope_input=safe(",".join(report["scope"])) if not report["full_schema"] else "",
        active_count=len(report["active"]), active_list=f"<ul>{active_rows}</ul>",
        derived_count=len(report["derived"]), derived_list=f"<ul>{derived_rows}</ul>",
        blocked_count=len(report["blocked"]), blocked_list=f"<ul>{blocked_rows}</ul>",
        confirm_ids=safe(",".join(d["id"] for d in report["derived"])),
        amend_disabled=locked or ("disabled" if not report["derived"] else ""),
        locked=locked)


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
                scope_blocked=plan["scope_blocked"],
                count=len(plan["items"]), items=entries(plan), scope=scope_panel(plan),
                history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions))
