from urllib.parse import urlencode

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
            if item["choice"] is not None:
                query = urlencode({"revision": plan["revision"], "item": item["id"]})
                form += (f'<a class="clear-link" href="/plans/{plan["id"]}/clear?{query}">'
                         "清空这条裁决的影响 →</a>")
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


def clear_page(preview):
    item = preview["item"]
    rows = "".join(fragment("clear-row", role=safe(label), value=values(item[role]),
                            current=" current" if role == preview["current_choice"] else "")
                   for role, label in ROLE_LABELS.items())
    candidate = preview["current_candidate"]
    if candidate["present"]:
        candidate_block = fragment(
            "clear-fact", tone="warn",
            title="当前版本已有通过重演的候选",
            detail=safe(f'候选摘要 {candidate["candidate_digest"]}；清空使版序推进至第 '
                        f'{preview["next_revision"]} 版，该候选不再属于当前版本，也不能用于封存。'))
    else:
        candidate_block = fragment(
            "clear-fact", tone="quiet",
            title="当前版本没有有效候选",
            detail=safe("当前版尚未通过重演；清空不会产生可下载候选，新版本仍须重新裁决并重演。"))
    evidence = preview["evidence"]
    if evidence["history_count"]:
        detail = (f'重演历史保留 {evidence["history_count"]} 条记录'
                  f'（其中 {evidence["passed_count"]} 条带通过候选，版序 '
                  f'{"、".join(map(str, evidence["revisions"]))}），清空不会删除任何历史证据。')
    else:
        detail = "该计划目前没有任何重演历史，清空不涉及证据删除。"
    evidence_block = fragment("clear-fact", tone="keep", title="既有重演证据全部保留", detail=safe(detail))
    return page("clear", "清空裁决预览", plan=preview["id"], short=preview["id"][:12],
                revision=preview["revision"], next_revision=preview["next_revision"],
                table=safe(item["table"]), key=values(item["key"]),
                choice=safe(preview["current_choice_label"]),
                unresolved=preview["after"]["unresolved"], rows=rows,
                reason=safe(preview["rehearsal"]["reason"]),
                candidate=candidate_block, evidence=evidence_block,
                item=item["id"])
