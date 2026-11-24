"""移植计划的网页渲染：逐行交代处置与来源，单列改名组完整性。"""

import json

from .port import GROUP_LABELS, PORT_ROLE_LABELS
from .render import fragment, option, page, safe, values

BADGE = {
    "auto": "auto", "already": "auto", "keep": "conflict",
    "reselect": "conflict", "pending": "conflict",
}


def port_entries(port):
    blocks = []
    for item in sorted(port["items"], key=lambda i: (i["disposition"] != "pending",
                                                     i["table"], canonical_key(i["key"]))):
        form = ""
        if item["automatic"] is None:
            options = option("clear", "保留未决", chosen=item["choice"] is None)
            options += "".join(option(role, label, chosen=role == item["choice"])
                               for role, label in PORT_ROLE_LABELS.items())
            form = fragment("port_decision", plan=port["id"], revision=port["revision"],
                            item=item["id"], options=options)
        source = PORT_ROLE_LABELS.get(item["source"], "—") if item["source"] else "—"
        badge = BADGE[item["disposition"]]
        blocks.append(fragment("port_entry", table=safe(item["table"]), key=values(item["key"]),
                               disposition=item["disposition_label"], source=safe(source),
                               badge=badge, form=form,
                               **{role: values(item[role]) for role in PORT_ROLE_LABELS}))
    return "".join(blocks) or '<div class="empty">三份镜像没有行差异。</div>'


def canonical_key(key):
    return json.dumps(key, ensure_ascii=True)


def rename_blocks(port, attempts):
    latest = attempts[0] if attempts else None
    groups = latest.get("rename_groups") if latest else None
    if not groups:
        groups = [{"id": g["id"], "table": g["table"], "status": g["status"],
                   "status_label": GROUP_LABELS[g["status"]], "old_key": g["old_key"],
                   "new_key": g["new_key"], "blocked_by": g.get("blocked_by")}
                  for g in port["groups"]]
    items = "".join(fragment(
        "port_group", table=safe(g["table"]),
        old=values(g["old_key"]), new=values(g["new_key"]),
        status=safe(g["status_label"]),
        badge=safe(g["status"]),
        reason=safe(g["blocked_by"]) if g.get("blocked_by") else "") for g in groups)
    return items or '<p class="muted">本计划没有复合主键改名。</p>'


def port_attempts_list(attempts):
    blocks = []
    for a in attempts:
        status = "通过" if a["status"] == "passed" else "受阻"
        detail = a.get("candidate_digest") or a["problem"]["detail"]
        groups = "、".join(f'{g["table"]} {g["status_label"]}' for g in a.get("rename_groups", []))
        blocks.append(fragment("port_attempt", revision=a["revision"], status=status,
                               applied=a.get("applied", 0), skipped=a.get("skipped", 0),
                               groups=safe(groups) or "无改名", evidence=safe(detail)))
    return "".join(blocks) or '<p class="muted">暂无重演记录。</p>'


def port_page(port, images, attempts):
    actions = []
    actions.append(fragment("port_action", plan=port["id"], revision=port["revision"],
                            action="rehearse", label="重演移植",
                            disabled=" disabled" if port["unresolved"] else ""))
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>'
                    for label, i in zip(PORT_ROLE_LABELS.values(), images))
    return page("port", "分支移植计划", plan=port["id"], short=port["id"][:12], roles=roles,
                revision=port["revision"], unresolved=port["unresolved"],
                count=len(port["items"]), items=port_entries(port),
                groups=rename_blocks(port, attempts),
                history=port_attempts_list(attempts), actions="".join(actions))
