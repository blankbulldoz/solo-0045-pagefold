from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values

CARRY_LABELS = {"retained": "承接保留", "needs_review": "需要重审",
                "automatic": "已自动化", "dropped": "条目消失"}
CARRY_BADGES = {"retained": "carry-ok", "needs_review": "carry-review",
                "automatic": "carry-auto", "dropped": "carry-gone"}


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def carry_index(plan):
    record = plan.get("succession")
    if record is None or record["relation"] != "successor":
        return {}
    return {(entry["table"], tuple(part[1] for part in entry["key"])): entry
            for entry in record["report"]["entries"]}


def entries(plan):
    blocks = []
    carried = carry_index(plan)
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
        carry = carried.get((item["table"], tuple(part[1] for part in item["key"])))
        badge = "auto" if auto else "conflict"
        carry_mark = ""
        if carry is not None:
            badge = CARRY_BADGES[carry["status"]]
            carry_mark = f' <em class="carry {CARRY_BADGES[carry["status"]]}">{CARRY_LABELS[carry["status"]]}</em>'
        blocks.append(fragment("entry", table=safe(item["table"]), key=values(item["key"]),
                               choice=chosen + carry_mark, badge=badge, form=form,
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


def succession_banner(plan):
    record = plan.get("succession")
    if record is None:
        return ""
    if record["relation"] == "predecessor":
        return fragment("succession-link", direction="已由承接计划接手",
                        other=record["other_plan"], detail="查看承接计划逐条核对结果 →")
    return fragment("succession-link", direction="承接自已归档计划",
                    other=record["other_plan"], detail="← 查看被归档的原计划")


def carry_form(plan, images):
    if plan["state"] == "archived":
        return ""
    selects = "".join(
        f'<label><span class="input-title">{label}</span>'
        f'<select name="{role}"><option value="">沿用当前{label}</option>'
        + "".join(f'<option value="{safe(i["id"])}">{safe(i["label"])} · {i["id"][:10]}</option>'
                  for i in images) + "</select></label>"
        for role, label in ROLE_LABELS.items())
    body = fragment("carry", plan=plan["id"], revision=plan["revision"], selects=selects)
    return ('<section class="paper carry-panel"><div class="panel-title">'
            '<h2>归档并承接（可选）</h2><span class="tag">更正祖本</span></div>' + body + "</section>")


def plan_page(plan, images, attempts, published):
    actions = []
    if plan["state"] == "open":
        passed = any(a["revision"] == plan["revision"] and a["status"] == "passed" for a in attempts)
        actions.append(action_form(plan, "rehearse", "重演当前版", "primary", bool(plan["unresolved"])))
        actions.append(action_form(plan, "seal", "封存成果", disabled=not passed))
    if plan["state"] != "archived":
        actions.append(action_form(plan, "archive", "仅归档（不承接）", "quiet"))
    if published:
        actions.append(fragment("release", plan=plan["id"]))
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>' for label, i in zip(ROLE_LABELS.values(), images))
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions), banner=succession_banner(plan), carry=carry_form(plan, images))
