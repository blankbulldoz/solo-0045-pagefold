from .render import page, safe, values

STATUS_LABELS = {
    "absent": "缺席/省略",
    "automatic": "自动采纳",
    "conflict": "待决",
}

ROLE_TEXT = {"base": "祖本", "left": "左支", "right": "右支", None: "—"}


def _options(selected, images):
    head = '<option value="">选择已登记镜像</option>'
    body = "".join(
        f'<option value="{safe(i["id"])}"{" selected" if i["id"] == selected else ""}>'
        f'{safe(i["label"])} · {safe(i["id"][:10])}</option>' for i in images)
    return head + body


def trial_form(images, selected):
    def field(name, title, desc):
        return (f'<label><span class="input-title">{title}</span>'
                f'<span class="input-desc">{desc}</span>'
                f'<select name="{name}" required>{_options(selected.get(name, ""), images)}</select></label>')
    return (
        '<form class="new-plan" method="get" action="/trials">'
        + field("left", "左支", "两个候选祖本共同对照的第一份分支")
        + field("right", "右支", "两个候选祖本共同对照的第二份分支")
        + field("base_a", "候选祖本 A", "上半份预览使用的祖本")
        + field("base_b", "候选祖本 B", "下半份预览使用的祖本")
        + '<button class="primary" type="submit">开始并列试算（不保存）</button></form>'
    )


def _aligned_block(row):
    sides = row["sides"]
    cells = []
    for side in sides:
        badge = {"absent": "quiet", "automatic": "auto", "conflict": "conflict"}[side["status"]]
        role = ROLE_TEXT[side["automatic"]]
        cells.append(
            f'<div class="trial-side"><span class="tag {badge}">{safe(side["label"])} · '
            f'{STATUS_LABELS[side["status"]]} · {role}</span>'
            f'<dl class="triptych"><div><dt>祖本</dt><dd>{values(side["base"])}</dd></div>'
            f'<div><dt>左支</dt><dd>{values(side["left"])}</dd></div>'
            f'<div><dt>右支</dt><dd>{values(side["right"])}</dd></div></dl></div>')
    flag = "实质不同" if row["materially_different"] else "实质相同"
    flip = "".join(
        f'<p class="muted">采纳翻转：{safe(f["from"])} 的 {ROLE_TEXT[f["role_from"]]} '
        f'→ {safe(f["to"])} 的 {ROLE_TEXT[f["role_to"]]}</p>' for f in row["automatic_flips"])
    return (f'<article class="entry trial-row"><div class="entry-head"><h3>{safe(row["table"])} '
            f'<span>{values(row["key"])}</span></h3><span class="tag">{flag}</span></div>'
            f'<div class="trial-sides">{"".join(cells)}</div>{flip}</article>')


def trial_page(images, selected, trial):
    if trial is None:
        return page("trial", "替代祖本并列试算", form=trial_form(images, selected),
                    summary="", previews="", aligned='<div class="empty">选择两个候选祖本后开始试算。</div>')
    comparison = trial["comparison"]
    preview_blocks = []
    for preview in trial["previews"]:
        rows = "".join(
            f'<tr><td>{safe(r["table"])}</td><td>{values(r["key"])}</td>'
            f'<td>{STATUS_LABELS[r["status"]]}</td><td>{ROLE_TEXT[r["automatic"]]}</td></tr>'
            for r in preview["rows"]) or '<tr><td colspan="4" class="muted">无差异项。</td></tr>'
        preview_blocks.append(
            f'<section class="paper"><div class="panel-title"><h2>{safe(preview["label"])}</h2>'
            f'<span class="tag">{preview["counts"]["items"]} 项 · '
            f'{preview["counts"]["conflict"]} 待决</span></div>'
            f'<table class="trial-table"><thead><tr><th>表</th><th>主键</th><th>状态</th>'
            f'<th>采纳角色</th></tr></thead><tbody>{rows}</tbody></table></section>')
    idseq = {row["id"]: row["key"][1][1] if len(row["key"]) > 1 and isinstance(row["key"][1], list)
             else row["id"][:8] for row in trial["aligned"]}
    flips = "、".join(str(idseq[i]) for i in comparison["automatic_flip_items"]) or "无"
    vanished = "、".join(str(idseq[i]) for i in comparison["vanished_items"]) or "无"
    appeared = "、".join(str(idseq[i]) for i in comparison["appeared_items"]) or "无"
    resolved = "、".join(str(idseq[e["id"]]) for e in comparison["conflict_resolved_items"]) or "无"
    summary = (
        f'<div class="trial-summary"><p>计划项总数：{safe(comparison["totals"])}'
        f'（{"相同" if comparison["same_total"] else "不同"}）</p>'
        f'<p>待决数：{safe(comparison["conflicts"])}</p>'
        f'<p>自动采纳翻转的主键：{safe(flips)}</p>'
        f'<p>下半份消失的行：{safe(vanished)}；新出现的行：{safe(appeared)}</p>'
        f'<p>待决被化解的主键：{safe(resolved)}</p>'
        f'<p>实质不同的行数：{comparison["materially_different_count"]}</p>'
        f'<p class="muted">本次试算不写入计划、裁决或重演记录。</p></div>')
    aligned = "".join(_aligned_block(row) for row in trial["aligned"]) \
        or '<div class="empty">两份预览没有任何主键差异。</div>'
    return page("trial", "替代祖本并列试算", form=trial_form(images, selected),
                summary=summary, previews="".join(preview_blocks), aligned=aligned)
