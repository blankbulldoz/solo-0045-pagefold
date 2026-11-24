from .plans import paged
from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def current(flag):
    return ' class="current" aria-current="page"' if flag else ""


def material_revision(view):
    return view["revision"] if view["revision"] is not None else view["current_revision"]


def filters(plan):
    view = plan["view"]
    revision = view["current_revision"]
    key = "pending" if view["pending"] else "conflicts"
    links = [f'<a{current(view["table"] is None)} href="{safe(paged(plan["id"], pending=view["pending"], limit=view["limit"], revision=revision))}">'
             f'全部表<span>{plan["counts"][key]}</span></a>']
    for table in plan["tables"]:
        links.append(f'<a{current(view["table"] == table["name"])} href="{safe(paged(plan["id"], table=table["name"], pending=view["pending"], limit=view["limit"], revision=revision))}">'
                     f'{safe(table["name"])}<span>{table[key]}</span></a>')
    toggle = "0" if view["pending"] else "1"
    label = "含已裁决的行" if view["pending"] else "只看待决的行"
    toggle_url = paged(plan["id"], table=view["table"], pending=toggle == "1",
                       limit=view["limit"], revision=revision)
    window = (f"第 {view['start'] + 1}–{view['end']} 项" if plan["items"] else "本页无行")
    scope = "只看待决" if view["pending"] else "含已裁决"
    return (f'<div class="filters-scroll"><nav class="filters" aria-label="按表翻阅">{"".join(links)}</nav></div>'
            f'<div class="filters-meta"><span>第 {revision} 版读取 · {scope} · {window}，筛选后 {view["total"]} 项</span>'
            f'<a href="{safe(toggle_url)}">{label}</a></div>')


def pager(plan):
    view = plan["view"]
    if view["limit"] is None:
        return ""
    revision = view["current_revision"]
    links = []
    if view["previous"] is not None:
        target = paged(plan["id"], table=view["table"], pending=view["pending"], limit=view["limit"],
                       cursor=view["previous"] or None, revision=revision)
        links.append(f'<a href="{safe(target)}">← 上一页</a>')
    position = f"第 {view['start'] + 1}–{view['end']} 项，共 {view['total']} 项" if plan["items"] \
        else f"已越过第 {view['start']} 项，其余 0 项"
    marker = "（前页位置已裁决）" if view["anchor_decided"] else ""
    if view["page"] is None:
        left = view["total"] - view["start"]
        position = f"第 {view['start'] + 1} 项起，剩余 {left} 项" if plan["items"] \
            else f"已越过第 {view['start']} 项，本表无后续待决行"
        links.append(f'<span>{marker}{position} · 续页</span>')
    else:
        links.append(f'<span>{marker}{position} · 第 {view["page"]}/{view["pages"]} 页</span>')
    if view["next"] is not None:
        target = paged(plan["id"], table=view["table"], pending=view["pending"], limit=view["limit"],
                       cursor=view["next"], revision=revision)
        links.append(f'<a href="{safe(target)}">下一页 →</a>')
    return "".join(links)


def remaining(plan):
    view = plan["view"]
    if view["table"] is None:
        return plan["counts"]["pending"]
    return view["scope_pending"]


def notice(plan):
    view = plan["view"]
    saved = plan["saved"]
    blocks = []
    if view["stale"]:
        target = paged(plan["id"], table=view["table"], pending=view["pending"], limit=view["limit"],
                       cursor=view["cursor"], revision=view["current_revision"])
        blocks.append(
            f'<section class="notice stale"><b>链接指向第 {view["revision"]} 版，计划现为第 {view["current_revision"]} 版。</b>'
            f'<p>下面的行与未决数按第 {view["current_revision"]} 版读取；页面内表单仍以第 {view["revision"]} 版提交，'
            f'保存会被拒绝为过期版本，请按新版本重新打开后再改。</p>'
            f'<a href="{safe(target)}">以第 {view["current_revision"]} 版重新打开本页</a></section>')
    if saved is not None:
        choice = ROLE_LABELS.get(saved["choice"], "保留未决")
        if not view["pending"]:
            detail = "当前为含已裁决视图，该行仍留在原位，可继续修改。"
        elif saved["pending"]:
            detail = "该项仍在待决集合中，本页从该项重新开始。"
        elif plan["items"]:
            detail = (f"该项已离开待决集合，本页改从它之后的下一待决行开始，"
                      f"当前为第 {view['start'] + 1}–{view['end']} 项（共 {view['total']} 项）。")
        elif remaining(plan):
            detail = (f"该项已离开待决集合，该表此位置之后没有待决行；"
                      f"计划中仍有 {plan['counts']['pending']} 项待决。")
        else:
            detail = "该项已离开待决集合；所选范围内已没有后续待决行。"
        blocks.append(f'<section class="notice saved"><b>已保存 {safe(saved["table"])} '
                      f'{values(saved["key"])} → {safe(choice)}</b><p>{detail}</p></section>')
    return "".join(blocks)


def empty_state(plan):
    view = plan["view"]
    counts = plan["counts"]
    revision = view["current_revision"]
    name = f"{safe(view['table'])} " if view["table"] else ""
    entry = next((t for t in plan["tables"] if t["name"] == view["table"]), None) if view["table"] else None
    conflicts = entry["conflicts"] if entry is not None else counts["conflicts"]
    target = paged(plan["id"], table=view["table"], pending=False, limit=view["limit"], revision=revision)
    if counts["items"] == 0:
        return '<div class="empty">三份镜像没有行差异。</div>'
    if not view["pending"]:
        return f'<div class="empty">{name}没有可显示的行。<a href="{safe(paged(plan["id"], table=view["table"], pending=True, limit=view["limit"], revision=revision))}">只看待决的行</a></div>'
    if conflicts == 0:
        return (f'<div class="empty">{name}没有需要裁决的差异，全部为自动采纳。'
                f'<a href="{safe(target)}">查看自动项</a></div>')
    if remaining(plan) == 0:
        return f'<div class="empty">{name}全部差异行均已裁决。<a href="{safe(target)}">查看含已裁决的行</a></div>'
    restart = paged(plan["id"], table=view["table"], pending=True, limit=view["limit"], revision=revision)
    return (f'<div class="empty">此位置之后，{name}没有待决行；计划中仍有 {counts["pending"]} 项待决。'
            f'<a href="{safe(restart)}">从该表开头继续</a> · <a href="{safe(target)}">查看该表全部行</a></div>')


def decision_form(plan, item):
    view = plan["view"]
    options = option("clear", "保留未决", chosen=item["choice"] is None)
    options += "".join(option(role, label, chosen=role == item["choice"])
                       for role, label in ROLE_LABELS.items())
    return fragment("decision", plan=plan["id"], revision=material_revision(view), item=item["id"],
                    options=options, table=safe(view["table"] or ""),
                    pending="1" if view["pending"] else "0",
                    limit="" if view["limit"] is None else view["limit"],
                    cursor=safe(view["cursor"] or ""))


def entries(plan):
    if not plan["items"]:
        return empty_state(plan)
    blocks = []
    for item in plan["items"]:
        auto = item["automatic"]
        form = ""
        if plan["state"] == "open" and auto is None:
            form = decision_form(plan, item)
        choice = ROLE_LABELS.get(item["choice"], "待裁决")
        chosen = ("自动 · " if auto else "") + choice
        blocks.append(fragment("entry", table=safe(item["table"]), key=values(item["key"]),
                               choice=chosen, badge="auto" if auto else "conflict", form=form,
                               **{role: values(item[role]) for role in ROLE_LABELS}))
    return "".join(blocks)


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
    view = plan["view"]
    position = (f"第 {view['start'] + 1}–{view['end']} 项" if plan["items"]
                else f"已越过第 {view['start']} 项")
    if view["page"] is not None:
        scope = f"{position}，筛选后共 {view['total']} 项，第 {view['page']}/{view['pages']} 页"
    else:
        scope = (f"从第 {view['start'] + 1} 项继续，筛选后共 {view['total']} 项，"
                 f"剩余 {view['total'] - view['start']} 项")
    count = (f"本页 {len(plan['items'])} 项 · 待决 {plan['counts']['pending']} 项 · "
             f"已裁决 {plan['counts']['decided']} 项")
    scope_label = "只看待决" if view["pending"] else "含已裁决"
    title = f"主键行差异 · {scope_label}"
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                scope=scope, panel=title, count=count, filters=filters(plan), items=entries(plan),
                pager=pager(plan), notice=notice(plan), history=attempts_list(attempts, plan["revision"]),
                actions="".join(actions))
