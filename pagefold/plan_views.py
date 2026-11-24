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


STATUS_LABELS = {"passed": "通过", "blocked": "受阻", "running": "处理中", "stale": "已失效"}


def attempt_evidence(attempt):
    status = attempt.get("status")
    if status == "passed":
        return attempt.get("candidate_digest", "")
    if status == "blocked":
        return attempt.get("problem", {}).get("detail", "")
    if status == "stale":
        return attempt.get("reason", {}).get("detail", "")
    return "输入已读取，正在重演。"


def attempts_list(attempts, revision):
    blocks = []
    for attempt in attempts:
        status = attempt.get("status", "blocked")
        current = revision == attempt["revision"]
        if status == "stale":
            marker = " · 已失效"
        elif status == "running":
            marker = " · 处理中"
        else:
            marker = " · 当前版" if current else " · 历史版"
        blocks.append(fragment("attempt", revision=attempt["revision"],
                               status=STATUS_LABELS.get(status, status), marker=marker,
                               evidence=safe(attempt_evidence(attempt)),
                               changes=attempt.get("changed", 0)))
    return "".join(blocks) or '<p class="muted">暂无重演记录。</p>'


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def plan_page(plan, images, attempts, published):
    actions = []
    if plan["state"] == "open":
        passed = any(a["revision"] == plan["revision"] and a["status"] == "passed" for a in attempts)
        running = any(a["revision"] == plan["revision"] and a["status"] == "running" for a in attempts)
        actions.append(action_form(plan, "rehearse", "重演当前版", "primary",
                                   bool(plan["unresolved"]) or running))
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
