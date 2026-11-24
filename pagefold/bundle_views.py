from .render import STATE_LABELS, fragment, option, page, safe


def plan_options(plans):
    return option("", "选择合流计划") + "".join(
        option(p["id"], f'{p["left_label"]} ＋ {p["right_label"]} · 第 {p["revision"]} 版 · {p["id"][:10]}')
        for p in plans)


def bundle_links(bundles):
    return "".join(f'<a class="plan-link" href="/bundles/{b["id"]}">'
                   f'<span>联合交付 <b>{b["id"][:12]}</b></span>'
                   f'<small>{b["members"]} 份数据库 · 同一套领取</small></a>' for b in bundles)


def member_entry(bundle_id, member):
    note = ""
    if member["current"] != member["revision"]:
        note = (f'<p class="muted">计划当前已推进至第 {member["current"]} 版，'
                f'本套仍锁定第 {member["revision"]} 版的候选。</p>')
    if member["sealed"]:
        credential = f'<a href="/api/plans/{member["plan"]}/certificate">原有封存凭证 ↓</a>'
    else:
        credential = '<span class="muted">计划尚未封存，上方成员记录即其来源凭证。</span>'
    return fragment("bundle_member", bundle=bundle_id, plan=member["plan"],
                    label=safe(member["label"]), revision=member["revision"],
                    state=STATE_LABELS[member["state"]], schema=member["schema"],
                    plan_digest=member["plan_digest"], decision_digest=member["decision_digest"],
                    candidate_digest=member["candidate_digest"],
                    size=f'{member["candidate_size"]:,}', note=note, credential=credential)


def bundle_page(view):
    manifest = view["manifest"]
    members = "".join(member_entry(manifest["id"], m) for m in view["members"])
    return page("bundle", "联合交付", short=manifest["id"][:12], bundle=manifest["id"],
                count=len(view["members"]), members=members)
