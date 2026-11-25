from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values

WRITE_LABELS = {"insert": "插入", "update": "修改", "delete": "删除", "none": "无写入"}


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


def impact_summary(impact):
    if not impact or impact.get("format") != "pagefold-impact-v1":
        return "影响范围待重演"
    writes = impact["totals"]["writes"]
    parts = [f'{WRITE_LABELS[kind]} {writes[kind]}' for kind in ("insert", "update", "delete")]
    return "、".join(parts) + f' · 写过 {impact["totals"]["written_tables"]} 张表'


def impact_panel(impact):
    if impact and impact.get("format") == "pagefold-impact-unknown":
        return f'<p class="muted">{safe(impact["detail"])}</p>'
    if not impact or impact.get("format") != "pagefold-impact-v1":
        return '<p class="muted">通过重演后在此查看逐表影响事实。</p>'
    rows = []
    for name, stats in impact["tables"].items():
        writes = stats["writes"]
        written = any(writes[kind] for kind in ("insert", "update", "delete"))
        badge = '<span class="tag">已写入</span>' if written else '<span class="tag quiet">未写入</span>'
        detail = "、".join(f'{WRITE_LABELS[kind]} {writes[kind]}'
                          for kind in ("insert", "update", "delete") if writes[kind]) or "无最终变动"
        rows.append(f'<li><b>{safe(name)}</b> {badge}<small>{detail} · 候选 {stats["rows"]} 行 '
                    f'<code>{stats["rows_digest"][:12]}</code></small></li>')
    return ('<div class="impact-head"><span class="tag">影响事实 '
            f'{safe(impact["format"])}</span><code>{safe(impact["rows_digest"])}</code></div>'
            '<ul class="impact-tables">' + "".join(rows) + "</ul>")


def attempts_list(attempts, revision):
    return "".join(fragment("attempt", revision=a["revision"],
                            status="通过" if a["status"] == "passed" else "受阻",
                            marker=" · 当前版" if revision == a["revision"] else " · 历史版",
                            evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
                            changes=a["changed"], impact=safe(impact_summary(a.get("impact")))
                            ) for a in attempts) or '<p class="muted">暂无重演记录。</p>'


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def plan_page(plan, images, attempts, published, impact=None):
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
                actions="".join(actions), impact=impact_panel(impact))
