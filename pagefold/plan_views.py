import json

from .diff_views import action_for, render_entry, table_meta
from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe


def decision_options(item):
    html = option("clear", "保留未决", chosen=item["choice"] is None)
    for role, label in ROLE_LABELS.items():
        chosen = " selected" if role == item["choice"] else ""
        action = action_for(item, role)
        html += (f'<option value="{role}" data-action="{action}" '
                 f'data-action-label="{safe(action)}"{chosen}>{safe(label)}</option>')
    return html


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def entries(plan, catalog=None):
    blocks = []
    for item in sorted(plan["items"], key=lambda item: bool(item["automatic"])):
        form = ""
        auto = item["automatic"]
        if plan["state"] == "open" and auto is None:
            options = decision_options(item)
            form = fragment("decision", plan=plan["id"], revision=plan["revision"],
                            item=item["id"], options=options)
        table = table_meta(item, catalog or {})
        blocks.append(render_entry(item, table, plan["state"], form))
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
    raw_catalog = images[0]["catalog"] if images else {}
    if isinstance(raw_catalog, str):
        try:
            catalog = json.loads(raw_catalog)
        except ValueError:
            catalog = {}
    elif isinstance(raw_catalog, dict):
        catalog = raw_catalog
    else:
        catalog = {}
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>' for label, i in zip(ROLE_LABELS.values(), images))
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan, catalog),
                history=attempts_list(attempts, plan["revision"]), actions="".join(actions))
