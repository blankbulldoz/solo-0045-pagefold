from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


ENTITY_KIND_LABELS = {"insert": "新增实体", "delete": "删除实体", "update": "内容变更", "rename": "主键改名"}
ENTITY_STATUS_LABELS = {"accepted": "已接受", "rejected": "已拒绝", "automatic": "自动采纳", "unresolved": "待裁决"}


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def entity_blocks(plan):
    blocks = []
    for entity in plan.get("entities", []):
        form = ""
        if plan["state"] == "open":
            options = option("clear", "清除明确裁决", chosen=not entity["explicit"] and entity["choice"] is None)
            options += "".join(option(role, label, chosen=entity["explicit"] and role == entity["choice"])
                                for role, label in (("left", "接受左支"), ("right", "接受右支"),
                                                    ("base", "拒绝改名/保留祖本")))
            form = (f'<form class="decision" action="/plans/{plan["id"]}/decisions" method="post">'
                    f'<input type="hidden" name="revision" value="{plan["revision"]}">'
                    f'<input type="hidden" name="entity" value="{entity["id"]}">'
                    f'<label>实体裁决 <select name="choice">{options}</select></label>'
                    f'<button>保存</button></form>')
        conflicts = entity.get("candidate_conflicts") or []
        warning = ""
        if conflicts:
            text = "；".join(f"{c['candidate_role']} 候选键在{c['observed_in']}支被另一实体占用"
                             for c in conflicts[:4])
            warning = (f'<p class="warning">⚠ {safe(text)}；'
                       '若占用实体不同步移走，重演会被约束拦截。</p>')
        status = ENTITY_STATUS_LABELS.get(entity["status"], entity["status"])
        badge = "conflict" if entity["status"] == "unresolved" else "auto"
        blocks.append(fragment(
            "entity", table=safe(entity["table"]), entity_key=values(entity["entity_key"]),
            status=safe(status), badge=badge,
            kind=safe(ENTITY_KIND_LABELS.get(entity["kind"], entity["kind"])),
            columns=safe(", ".join(entity["entity_columns"])), entity_id=safe(entity["id"][:16]),
            base_key=values(entity["keys"]["base"]), left_key=values(entity["keys"]["left"]),
            right_key=values(entity["keys"]["right"]), warning=warning, form=form))
    return "".join(blocks)


def entries(plan):
    blocks = []
    spec_tables = {spec["table"] for spec in plan.get("entity_keys", [])}
    for item in sorted(plan["items"], key=lambda item: bool(item["automatic"])):
        form = ""
        governed_by_entity = item.get("reference_driven") or item["table"] in spec_tables
        auto = item["automatic"]
        if plan["state"] == "open" and auto is None and not governed_by_entity:
            options = option("clear", "保留未决", chosen=item["choice"] is None)
            options += "".join(option(role, label, chosen=role == item["choice"])
                               for role, label in ROLE_LABELS.items())
            form = fragment("decision", plan=plan["id"], revision=plan["revision"], item=item["id"], options=options)
        if governed_by_entity and auto is None:
            choice = "跟随实体组"
        else:
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
    entity_html = entity_blocks(plan)
    entity_panel = (
        '<div class="panel-title"><h2>实体与改名裁决</h2>'
        f'<span class="tag">{len(plan.get("entities", []))} 组</span></div>'
        f'<div class="entries entity-groups">{entity_html}</div>'
    ) if entity_html else ""
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), entity_panel=entity_panel, items=entries(plan),
                history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions))
