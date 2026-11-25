from .audit import CHOICE_LABELS, STATUS_LABELS
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
    blocks = []
    for a in attempts:
        marker = " · 当前版" if revision == a["revision"] else " · 历史版"
        if a["record"] == "legacy":
            marker += " · 升级前旧记录"
            detail = f"/plans/{a['id']}/attempts/{a['revision']}"
        else:
            detail = f"/plans/{a['id']}/attempts/{a['revision']}"
        blocks.append(fragment(
            "attempt", revision=a["revision"],
            status=STATUS_LABELS.get(a["status"], a["status"]), marker=marker,
            evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
            changes=a["changed"], detail=detail,
            digest=a.get("decision_digest") or "无（旧记录未保存裁决细节）"))
    return "".join(blocks) or '<p class="muted">暂无重演记录。</p>'


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
                actions="".join(actions), audit=f'/plans/{plan["id"]}/audit')


def choice_rows(attempt):
    if attempt["choices"] is None:
        return ('<p class="muted">该记录写于升级之前，只固定了重演事实（版本、状态、候选摘要），'
                '没有保存逐项裁决细节，因此无法从这里倒推当时的选项。</p>')
    rows = []
    for entry in attempt["choices"]:
        origin = "自动" if entry["automatic"] else "人工"
        if not entry["automatic"] and not entry["decided"]:
            origin = "人工 · 已清空"
        rows.append(fragment(
            "choice-row", table=safe(entry["table"]), key=safe(" / ".join(map(str, entry["key"]))),
            origin=origin, choice=safe(CHOICE_LABELS.get(entry["choice"], str(entry["choice"])))))
    return "".join(rows)


def attempt_page(attempt):
    if attempt["record"] == "legacy":
        banner = '<p class="tag conflict">升级前旧记录：无选择快照</p>'
        digest = "无（旧记录未保存裁决细节）"
        requested = "未知（旧记录）"
        sequence = "—"
    else:
        banner = '<p class="tag auto">新格式记录：选项与版本已固定</p>'
        digest = attempt["decision_digest"]
        requested = attempt["requested_at"]
        sequence = attempt["sequence"]
    evidence = attempt.get("candidate_digest") or attempt["problem"]["detail"]
    return page(
        "attempt_detail", "重演记录", plan=attempt["id"], revision=attempt["revision"],
        status=STATUS_LABELS.get(attempt["status"], attempt["status"]), changes=attempt["changed"],
        evidence=safe(evidence), banner=banner, digest=safe(digest), requested=safe(requested),
        sequence=sequence, choices=choice_rows(attempt),
        audit=f'/plans/{attempt["id"]}/audit')


def event_rows(events):
    if not events:
        return '<p class="muted">没有裁决写入事件。</p>'
    blocks = []
    for event in events:
        effect = "推进版序" if event["advanced"] else "同版无变化（幂等重试）"
        blocks.append(fragment(
            "event-row", seq=event["seq"], revision=event["revision"], item=safe(event["item"]),
            choice=safe(CHOICE_LABELS.get(event["choice"], str(event["choice"]))),
            effect=effect, recorded=safe(event["recorded_at"])))
    return "".join(blocks)


def attempt_index(attempts):
    blocks = []
    for a in attempts:
        kind = "旧记录" if a["record"] == "legacy" else "新记录"
        digest = a.get("decision_digest") or "未保存裁决细节"
        blocks.append(fragment(
            "dossier-attempt", plan=a["id"], revision=a["revision"],
            status=STATUS_LABELS.get(a["status"], a["status"]), kind=kind,
            changes=a["changed"], digest=safe(digest)))
    return "".join(blocks) or '<p class="muted">暂无重演记录。</p>'


def audit_page(data):
    release = data["release"]
    if release:
        release_block = fragment(
            "dossier-release", plan=data["id"], revision=release["revision"],
            digest=safe(release["candidate_digest"]), decision=safe(release["decision_digest"]))
    else:
        release_block = '<p class="muted">计划尚未封存，没有凭证。</p>'
    archived = "已归档（只读，下列证据仍可回查）" if data["archived"] else STATE_LABELS[data["state"]]
    return page(
        "audit", "案卷索引", plan=data["id"], short=data["id"][:12], state=archived,
        revision=data["revision"], legacy=data["legacy_count"],
        plan_digest=safe(data["plan_digest"]),
        current=data["current_attempt"]["revision"] if data["current_attempt"] else "无",
        attempts=attempt_index(data["attempts"]), events=event_rows(data["events"]),
        release=release_block)
