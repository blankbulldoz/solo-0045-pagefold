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


def compare_picker(plan_id, attempts):
    if not attempts:
        return '<p class="muted">暂无重演记录，重演后可在此比较两版差异。</p>'
    revisions = [a["revision"] for a in attempts]
    newest = max(revisions)
    older = max((rev for rev in revisions if rev < newest), default=newest)
    options_a = "".join(option(rev, f"第 {rev} 版", chosen=rev == older) for rev in revisions)
    options_b = "".join(option(rev, f"第 {rev} 版", chosen=rev == newest) for rev in revisions)
    return (f'<form class="history-compare" method="get" action="/plans/{plan_id}/compare">'
            f'<label>比较<select name="a">{options_a}</select></label>'
            f'<span>与</span><label><select name="b">{options_b}</select></label>'
            '<button type="submit">比较</button>'
            f'<a class="compare-all" href="/plans/{plan_id}/compare?a={older}&amp;b={newest}">打开比较页</a></form>')


def attempts_list(attempts, revision, plan_id):
    blocks = []
    for index, attempt in enumerate(attempts):
        rev = attempt["revision"]
        if index + 1 < len(attempts):
            other = attempts[index + 1]["revision"]
            compare = (f'<a class="cmp-mini" href="/plans/{plan_id}/compare?a={other}&amp;b={rev}">'
                       "与前版比较</a>")
        else:
            compare = (f'<a class="cmp-mini" href="/plans/{plan_id}/compare?a={rev}&amp;b={rev}">'
                       "同版对照</a>")
        blocks.append(fragment("attempt", revision=rev,
                               status="通过" if attempt["status"] == "passed" else "受阻",
                               marker=" · 当前版" if revision == rev else " · 历史版",
                               evidence=safe(attempt.get("candidate_digest") or attempt["problem"]["detail"]),
                               changes=attempt["changed"], compare=compare))
    history = "".join(blocks) or '<p class="muted">暂无重演记录。</p>'
    return history + compare_picker(plan_id, attempts)


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
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"], plan["id"]),
                actions="".join(actions))
