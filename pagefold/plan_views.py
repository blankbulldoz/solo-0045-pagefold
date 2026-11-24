from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


def bridge_section(bridge):
    if not bridge:
        return ""
    report = bridge["report"]
    grouped = {}
    for entry in report["tables"]:
        grouped.setdefault(entry["table"], []).append(entry)
    rows = []
    for table, entries in grouped.items():
        for number, entry in enumerate(entries):
            parts = [f"{entry['rows']} 行"]
            if entry["source"] != table:
                parts.append(f"表 {entry['source']} → {table}")
            if entry["renamed"]:
                parts.append("改名 " + "、".join(f"{src}→{dst}" for src, dst in entry["renamed"].items()))
            if entry["filled"]:
                parts.append("填充 " + "、".join(entry["filled"]))
            if entry["reordered"]:
                parts.append("列次序已对齐")
            if entry["keys_remapped"]:
                parts.append(f"{entry['keys_remapped']} 键重排")
            head = f'<th rowspan="{len(entries)}">{safe(table)}</th>' if not number else ""
            rows.append(f'<tr>{head}<th>{ROLE_LABELS[entry["role"]]}</th><td>{safe("；".join(parts))}</td></tr>')
    alterations = "".join(f'<li><code>{safe(sql)}</code></li>' for sql in report["alterations"])
    preserved = report["identity"].get("ancestor_rows_preserved")
    identity = "；".join(f"{ROLE_LABELS[role]} " + "、".join(
        f"{table} {counts['rows_in']}→{counts['rows_out']} 行"
        for table, counts in tables.items()) for role, tables in report["identity"].items() if role in ROLE_LABELS)
    return fragment("bridge", target=ROLE_LABELS[report["target"]], schema=safe(report["schema"][:16]),
                    ancestor=safe(bridge["ancestor"]), tables="".join(rows),
                    alterations=f"<ul>{alterations}</ul>" if alterations else "<p>祖本无需物理迁移。</p>",
                    identity=safe(identity),
                    preserved="一致" if preserved else ("未验证" if preserved is None else "不一致"))


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
                bridge=bridge_section(plan["bridge"]), actions="".join(actions))
