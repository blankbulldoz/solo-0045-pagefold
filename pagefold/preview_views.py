from .render import ROLE_LABELS, page, safe


def _role_cards(preview):
    cards = []
    for role in ("base", "left", "right"):
        record = preview["roles"][role]
        cards.append(
            f'<li class="preview-role" data-role="{role}">'
            f'<small>{ROLE_LABELS[role]}</small>'
            f'<b class="role-label">{safe(record["label"])}</b>'
            f'<code class="role-digest" title="{record["id"]}">{record["id"]}</code>'
            f'<span class="role-size">{record["size"]:,} B · 按字节 SHA-256 寻址</span>'
            f'</li>')
    return "".join(cards)


def _stat_tiles(preview):
    est = preview["estimate"]
    tiles = [
        ("差异项", est["items"], "计划将包含的主键行差异总数"),
        ("自动采纳", est["automatic"], "单边变化或双边同值，无需裁决"),
        ("待裁决冲突", est["conflicts"], "双边不同，必须人工选择"),
        ("涉及关系", est["tables"], "存在差异的表数量"),
        ("预估落库改动", est["estimated_row_writes"], "按自动选择计算的行写入数（不含未决冲突）"),
    ]
    return "".join(
        f'<li class="stat"><b>{value}</b><span>{safe(label)}</span><small>{safe(hint)}</small></li>'
        for label, value, hint in tiles)


def _change_table(preview):
    ct = preview["change_types"]
    rows = "".join(
        f"<tr><th scope=\"row\">{label}</th><td>{ct[key]}</td></tr>"
        for key, label in (("added", "新增（祖本缺席的键）"),
                           ("deleted", "删除（分支移除祖本行）"),
                           ("modified", "修改（祖本行的值变化）")))
    rows += (f'<tr><th scope="row">自动采纳</th><td>{preview["resolution"]["automatic"]}</td></tr>'
             f'<tr><th scope="row">冲突待裁决</th><td>{preview["resolution"]["conflict"]}</td></tr>')
    per_table = "".join(
        f'<tr><td>{safe(row["table"])}</td><td>{row["items"]}</td>'
        f'<td>{row["added"]}</td><td>{row["deleted"]}</td><td>{row["modified"]}</td>'
        f'<td>{row["automatic"]}</td><td>{row["conflict"]}</td></tr>'
        for row in preview["tables"])
    return (
        '<div class="change-columns"><table class="change-total"><tbody>' + rows + '</tbody></table>'
        '<div class="scroll"><table class="change-per-table"><thead><tr>'
        '<th>关系</th><th>差异</th><th>新增</th><th>删除</th><th>修改</th><th>自动</th><th>冲突</th>'
        '</tr></thead><tbody>' + (per_table or '<tr><td colspan="7" class="muted">无差异</td>') +
        '</tbody></table></div></div>')


def _scale_panel(preview):
    scale = preview["scale"]
    large = scale["large"]
    banner_class = "scale-banner large" if large else "scale-banner direct"
    if large:
        advice = (
            "<h3>已超过直接审阅规模</h3>"
            f"<p>{safe(scale['message'])}</p>"
            "<ul><li><b>仍要创建</b>：使用下方主按钮生成正式计划，随后在计划页逐项裁决 "
            f"{preview['estimate']['conflicts']} 项冲突。</li>"
            "<li><b>返回调整角色</b>：核对三份镜像的角色与摘要，修正看错的来源顺序。</li>"
            "<li><b>缩小范围</b>：取消本次创建，按表或批次拆分大库、重新登记后再次预览，"
            "避免提交过大的计划后再删除重做。</li></ul>"
        )
    else:
        advice = (
            "<h3>处于可直接审阅规模</h3>"
            f"<p>{safe(scale['message'])}确认后可直接在计划页审阅全部差异。</p>"
        )
    repeated = ""
    if preview["repeated_labels"]:
        names = "、".join(f"“{safe(name)}”" for name in preview["repeated_labels"])
        repeated = (
            '<p class="repeat-warning" role="alert">⚠ 检测到标签重复但字节不同的镜像：'
            f'{names}。标签不可作为来源依据，请逐字核对每张角色卡上的 SHA-256 摘要；'
            "确认凭证只绑定这些摘要，互换镜像不会写错来源。</p>")
    return f'<div class="{banner_class}" data-large="{str(large).lower()}">{advice}{repeated}</div>'


def preview_page(preview):
    token = preview["preview_token"]
    hidden_sources = "".join(
        f'<input type="hidden" name="{role}" value="{preview["sources"][role]}">'
        for role in ROLE_LABELS)
    large = preview["scale"]["large"]
    confirm_label = "仍要创建正式计划" if large else "确认无误，创建正式计划"
    return page(
        "preview", "创建前预览确认",
        token=token,
        token_short=token[:12],
        large_marker=" large" if large else "",
        roles=_role_cards(preview),
        stats=_stat_tiles(preview),
        changes=_change_table(preview),
        scale=_scale_panel(preview),
        hidden_sources=hidden_sources,
        confirm_label=confirm_label,
        digest=preview["plan_digest"][:16],
        total=preview["total"],
        conflicts=preview["estimate"]["conflicts"],
    )
