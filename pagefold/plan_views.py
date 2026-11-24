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


def attempts_list(attempts, revision):
    return "".join(fragment("attempt", revision=a["revision"],
                            status="通过" if a["status"] == "passed" else "受阻",
                            marker=" · 当前版" if revision == a["revision"] else " · 历史版",
                            evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
                            changes=a["changed"]) for a in attempts) or '<p class="muted">暂无重演记录。</p>'


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def short_digest(digest, length=18):
    if not digest:
        return "—"
    return safe(digest[:length] + "…")


def source_list(sources, source_roles=None):
    """Render role -> image label/id rows used by both panels and comparisons."""
    labels = {entry["role"]: entry for entry in (source_roles or [])}
    rows = []
    for role, role_label in ROLE_LABELS.items():
        image_id = (sources or {}).get(role, "—")
        meta = labels.get(role)
        label = safe(meta["label"]) if meta else "镜像"
        rows.append(
            f'<li class="source-row role-{role}"><span class="role-label">{role_label}</span>'
            f'<span class="source-label">{label}</span><code>{safe(image_id)}</code></li>')
    return "".join(rows)


def seal_panel(plan, preview, certificate):
    plan_id = plan["id"]
    if certificate:
        sources = certificate["sources"]
        return fragment(
            "sealed", revision=certificate["revision"], digest=certificate["candidate_digest"],
            size=certificate.get("candidate_size", "—"), format=safe(certificate["format"]),
            badge=f"{STATE_LABELS.get(plan['state'], '')} · 第 {certificate['revision']} 版",
            sources=source_list(sources), links=fragment("release", plan=plan_id))
    if preview and preview.get("sealable"):
        sources = preview["sources"]
        return fragment(
            "confirm", plan=plan_id, revision=preview["revision"],
            digest=preview["candidate_digest"], digest_short=short_digest(preview["candidate_digest"], 16),
            size=preview["candidate_size"], plan_digest=preview["plan_digest"],
            decision_digest=preview["decision_digest"],
            base=sources["base"], left=sources["left"], right=sources["right"],
            sources=source_list(sources, preview.get("source_roles")))
    return fragment("waiting")


def plan_page(plan, images, attempts, published, preview=None, certificate=None):
    actions = []
    if plan["state"] == "open":
        passed = any(a["revision"] == plan["revision"] and a["status"] == "passed" for a in attempts)
        actions.append(action_form(plan, "rehearse", "重演当前版", "primary", bool(plan["unresolved"])))
        if not passed:
            actions.append(action_form(plan, "seal", "封存成果", disabled=True))
        actions.append(action_form(plan, "archive", "归档计划", "quiet"))
    elif plan["state"] != "archived":
        actions.append(action_form(plan, "archive", "归档计划", "quiet"))
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>' for label, i in zip(ROLE_LABELS.values(), images))
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions), sealpanel=seal_panel(plan, preview, certificate))


def evidence_card(title, obj, tone):
    if not obj:
        return ""
    digest = obj.get("candidate_digest")
    if digest:
        digest_row = f'<div class="cmp-digest"><dt>候选摘要</dt><dd><code>{safe(digest)}</code></dd></div>'
    else:
        latest = obj.get("latest_attempt")
        extra = ""
        if latest:
            extra = f'（最近重演：第 {latest["revision"]} 版 · {safe(latest.get("status") or "—")}）'
        digest_row = f'<div class="cmp-digest"><dt>候选摘要</dt><dd class="muted">当前版尚无通过候选{extra}</dd></div>'
    size = obj.get("candidate_size")
    size_row = f"<div><dt>大小</dt><dd>{safe(size)} 字节</dd></div>" if size else ""
    state = obj.get("state")
    state_row = f"<div><dt>状态</dt><dd>{safe(STATE_LABELS.get(state, state))}</dd></div>" if state else ""
    href = obj.get("href") or (f'/plans/{obj["plan"]}' if obj.get("plan") else None)
    raw_plan = str(obj.get("plan") or "—")
    plan_value = safe(raw_plan[:20] + ("…" if len(raw_plan) > 20 else ""))
    if href:
        plan_row = f'<div><dt>计划</dt><dd><a href="{safe(href)}"><code>{plan_value}</code></a></dd></div>'
    else:
        plan_row = f'<div><dt>计划</dt><dd><code>{plan_value}</code></dd></div>'
    return (
        f'<div class="compare-col {tone}"><h3>{safe(title)}</h3><dl class="evidence cmp-evidence">'
        f'{plan_row}<div><dt>版序</dt><dd>第 {safe(obj.get("revision", "—"))} 版</dd></div>'
        f'{state_row}{digest_row}{size_row}'
        f'<div class="cmp-sources"><dt>来源角色</dt><dd><ul class="confirm-sources compact">'
        f'{source_list(obj.get("sources"), obj.get("source_roles"))}</ul></dd></div>'
        f'</dl></div>'
    )


def comparison_panel(problem):
    """Render the old confirmation vs the new comparison object (or cross-plan)."""
    details = getattr(problem, "details", {}) or {}
    code = getattr(problem, "code", "")
    if code == "stale_confirmation" and details.get("current"):
        columns = evidence_card("你确认的旧证据", details.get("confirmed"), "old")
        columns += evidence_card("当前新的比较对象", details.get("current"), "new")
        current = details["current"]
        link = f'/plans/{current["plan"]}'
        links = f'<a class="primary button-link" href="{safe(link)}">打开当前计划，重新核对并确认 →</a>'
        return fragment("compare", tone="stale", heading="确认已过期：请对照新的比较对象",
                        message=safe(problem.detail), columns=columns, links=links)
    if code == "confirmation_plan_mismatch":
        columns = evidence_card("这份确认实际指向", details.get("other_plan"), "other")
        columns += evidence_card("你提交到的当前计划", details.get("current"), "current")
        other = details.get("other_plan") or {}
        links = f'<a class="button-link" href="/plans/{safe(details.get("plan", ""))}">返回当前计划</a>'
        if other.get("href"):
            links += f'<a class="primary button-link" href="{safe(other["href"])}">前往确认所属的另一计划 →</a>'
        return fragment("compare", tone="cross", heading="确认指向另一个计划",
                        message=safe(problem.detail), columns=columns, links=links)
    return ""
