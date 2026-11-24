from urllib.parse import urlencode

from .render import ROLE_LABELS, STATE_LABELS, option, page, safe, typed, values


def short(value, length=12):
    return safe(value[:length]) if value else "—"


def image_options(images):
    options = option("", "选择已登记镜像")
    groups = {}
    for image in images:
        groups.setdefault(image["content_digest"], []).append(image["id"])
    for image in images:
        twins = len(groups.get(image["content_digest"], ())) - 1
        note = f" · 文件 {image['id'][:10]} · 内容 {image['content_digest'][:8] if image['content_digest'] else '—'}"
        if twins:
            note += f" · {twins} 件碎片等价"
        options += option(image["id"], f"{image['label']}{note}")
    return options


def image_links(images):
    groups = {}
    for image in images:
        groups.setdefault(image["content_digest"], []).append(image["id"])
    items = []
    for i in images:
        twins = len(groups.get(i["content_digest"], ())) - 1
        badge = f'<em class="identity-badge eq" title="与其他登记件共享内容摘要">碎片等价 ×{twins}</em>' if twins else ""
        items.append(
            f'<li><a href="/images/{i["id"]}">{safe(i["label"])}</a>'
            f'<small>{i["size"]:,} B</small>'
            f'<div class="digests"><code>文件 {i["id"][:12]}</code>'
            f'<code class="content-id">内容 {short(i["content_digest"])}</code>{badge}</div></li>')
    return "".join(items)


def identity_panel(image):
    return (
        '<section class="paper identity-card"><div class="panel-title"><h2>两种身份</h2>'
        '<span class="tag">文件摘要用于凭证</span></div><dl class="meta identity-meta">'
        '<dt>文件身份 · 精确字节 SHA-256</dt><dd><code>' + safe(image["id"]) + '</code>'
        '<small>VACUUM、空闲页整理或头部计数变化都会产生新的文件摘要；计划来源与封存凭证始终精确绑定它。</small></dd>'
        '<dt>内容身份 · 模式 + 类型化行</dt><dd><code>' + safe(image["content_digest"] or "—") + '</code>'
        '<small>对模式签名和保留存储类型的全部行计算；VACUUM 后目录与内容不变时保持一致。'
        '整数与文本、NULL 与空 BLOB 不会被当作相同内容。</small></dd>'
        '<dt>模式摘要</dt><dd><code>' + safe(image["catalog"]["signature"]) + '</code></dd>'
        '</dl></section>'
    )


def equivalents_section(image):
    rows = image.get("equivalents", [])
    if rows:
        items = "".join(
            f'<li class="relation eq"><div class="relation-head">'
            f'<a href="/images/{other["id"]}"><b>{safe(other["label"])}</b></a>'
            f'<span class="tag auto">碎片整理后等价</span></div>'
            f'<dl><dt>文件摘要</dt><dd><code>{other["id"]}</code></dd>'
            f'<dt>内容摘要</dt><dd><code>{safe(other["content_digest"])}</code>（与本镜像相同）</dd></dl>'
            f'<p><a href="/images/compare?a={image["id"]}&amp;b={other["id"]}">查看类型化逐行对照 →</a></p></li>'
            for other in rows)
    else:
        items = ('<li class="muted relation-empty">暂无其他共享内容摘要的登记件。'
                 '把本镜像 VACUUM 后重新登记：文件摘要会变，内容摘要仍相同，并在此处归组。</li>')
    return (f'<section class="paper relation-panel"><div class="panel-title">'
            f'<h2>碎片整理后等价的来源</h2><span class="tag">{len(rows)} 件</span></div>'
            f'<ul class="relation-list">{items}</ul>'
            '<p class="muted relation-note">内容等价不会合并文件身份：历史计划与凭证只绑定登记时选定的精确文件摘要，'
            '旧镜像的使用记录和凭证仍保留在旧摘要页面。</p></section>')


def lookalikes_section(image):
    rows = image.get("type_lookalikes", [])
    if rows:
        cards = []
        for other in rows:
            chips = "".join(
                f'<span class="type-chip" title="{safe(change["table"])} 第 {change["position"] + 1} 行 · 列 {safe(change["column"])}">'
                f'{safe(change["table"])}.{safe(change["column"])} '
                f'<i>{safe(change["kind_a"])}</i>→<i>{safe(change["kind_b"])}</i></span>'
                for change in other["type_changes"])
            cards.append(
                f'<li class="relation shifted"><div class="relation-head">'
                f'<a href="/images/{other["id"]}"><b>{safe(other["label"])}</b></a>'
                f'<span class="tag conflict">行位置相同但类型不同 · {other["type_change_count"]} 处</span></div>'
                f'<dl><dt>文件摘要</dt><dd><code>{other["id"]}</code></dd>'
                f'<dt>内容摘要</dt><dd><code>{short(other["content_digest"], 24)}</code>（不同，故不等价）</dd></dl>'
                f'<div class="type-chips">{chips}</div>'
                f'<p><a href="{safe(other["compare"])}">打开逐单元格对照 →</a></p></li>')
        body = "".join(cards)
    else:
        body = ('<li class="muted relation-empty">暂无“模式、行数与行位置相同，但存储类型不同”的登记件。'
                '例如相同位置上的整数 <code>1</code> 与文本 <code>"1"</code>、NULL 与空 BLOB，'
                '都会让内容摘要不同并在此提示。</li>')
    return (f'<section class="paper relation-panel"><div class="panel-title">'
            f'<h2>行位置相同但类型不同的来源</h2><span class="tag">{len(rows)} 件</span></div>'
            f'<ul class="relation-list">{body}</ul></section>')


def _usage_item(used, attribution=""):
    roles = "、".join(ROLE_LABELS[role] for role in used["roles"])
    cert = ""
    if used["certificate"]:
        cert = (f'<a class="cert-link" href="{used["certificate"]["url"]}">凭证 ↓</a>'
               f'<code title="凭证候选摘要">候选 {short(used["certificate"]["candidate_digest"], 16)}</code>')
    else:
        cert = '<span class="muted">尚未封存颁证</span>'
    return (
        f'<li><a href="{used["url"]}">计划 <code>{used["id"][:12]}</code></a>'
        f'<span class="tag">{STATE_LABELS[used["state"]]}</span>'
        f'<small>{attribution}担任{roles} · 第 {used["revision"]} 版</small>'
        f'<span class="cert-line">{cert}</span></li>')


def usage_section(image):
    plans = image.get("usage", [])
    equivalent_plans = image.get("equivalent_usage", [])
    own = (f'<ul class="usage-list">{"".join(_usage_item(used) for used in plans)}</ul>'
           if plans else '<p class="muted usage-empty">该文件摘要尚未被计划使用。</p>')
    inherited = ""
    if equivalent_plans:
        items = "".join(
            _usage_item(used, f'来自内容等价的旧登记件 <a href="/images/{used["image_id"]}">'
                              f'{safe(used["image_label"])} <code>{used["image_id"][:10]}</code></a> · ')
            for used in equivalent_plans)
        inherited = (
            '<div class="equivalent-history"><h3>内容等价的旧登记件历史来源</h3>'
            '<p class="muted">下列计划与凭证绑定的是另一文件摘要，不会因本镜像登记而改变；'
            '此处仅按相同内容摘要汇集，便于追溯。</p>'
            f'<ul class="usage-list">{items}</ul></div>')
    return ('<section class="paper relation-panel"><div class="panel-title">'
            '<h2>使用该镜像的计划与历史凭证</h2></div>' + own + inherited + '</section>')


def image_page(image):
    tables = []
    for table in image["catalog"]["tables"]:
        url = f'/images/{image["id"]}/rows?' + urlencode({"table": table["name"]})
        references = "、".join(f"{row[3]} → {row[2]}.{row[4] or '主键'}" for row in table["foreign_keys"]) or "—"
        tables.append(f'<tr><th><a href="{safe(url)}">{safe(table["name"])}</a></th>'
                      f'<td>{safe(" · ".join(table["key"]))}</td><td>{safe(references)}</td>'
                      f'<td>{table.get("rows", "—")}</td></tr>')
    relations = ('<div class="identity-grid">' + equivalents_section(image)
                 + lookalikes_section(image) + "</div>" + usage_section(image))
    return page("image", image["label"], label=safe(image["label"]), image_id=image["id"],
                content_digest=safe(image["content_digest"] or "—"),
                identity_panel=identity_panel(image), relations=relations,
                signature=image["catalog"]["signature"], tables="".join(tables),
                size=f'{image["size"]:,}')


def rows_page(image, result):
    table = result["table"]
    offset, total = result["offset"], result["total"]
    columns = "".join(f'<th scope="col">{safe(col)}</th>' for col in table["columns"])
    rows = "".join('<tr>' + "".join(f'<td>{typed(v)}</td>' for v in row["values"]) + '</tr>'
                   for row in result["rows"])
    nav = []
    for label, target, exists in [("上一页", max(0, offset - 50), offset > 0),
                                  ("下一页", offset + 50, offset + 50 < total)]:
        if exists:
            params = urlencode({"table": table["name"], "offset": target})
            nav.append(f'<a href="?{safe(params)}">{label}</a>')
    return page("rows", table["name"], name=safe(table["name"]), label=safe(image["label"]),
                image_id=image["id"], columns=columns, rows=rows, nav="".join(nav), total=total,
                window=f"{offset + 1}–{min(offset + 50, total)}" if rows else "当前页无行")


VERDICT_BADGE = {
    "same_file": ("auto", "摘要完全一致"),
    "defragmented_equivalent": ("auto", "文件字节不同 · 类型化内容等价"),
    "type_shifted_lookalike": ("conflict", "不可视为等价：存储类型不同"),
    "different": ("conflict", "类型化内容不同"),
}


def compare_page(report):
    badge_class, badge_text = VERDICT_BADGE.get(report["verdict"], ("conflict", report["verdict_label"]))
    cards = "".join(
        f'<div class="compare-side"><h3>{side}</h3>'
        f'<a href="/images/{who["id"]}"><b>{safe(who["label"])}</b></a>'
        f'<dl><dt>文件摘要</dt><dd><code>{who["id"]}</code></dd>'
        f'<dt>内容摘要</dt><dd><code>{safe(who["content_digest"] or "—")}</code></dd></dl></div>'
        for side, who in (("A", report["a"]), ("B", report["b"])))
    sections = []
    for table_report in report["tables"]:
        if not table_report["differences"]:
            body = '<p class="muted compare-clean">该表在对应行位置上没有单元格差异（含类型）。</p>'
        else:
            lines = []
            for diff in table_report["differences"]:
                if diff.get("presence"):
                    lines.append(
                        f'<tr class="presence"><td>{diff["position"] + 1}</td><td>—</td><td>—</td>'
                        f'<td colspan="2">{"仅 B 存在该行" if diff["presence"] == "right_only" else "仅 A 存在该行"}</td></tr>')
                    continue
                badge = ('<span class="tag conflict">类型变更</span>' if diff["type_change"]
                         else '<span class="tag">值不同</span>')
                if isinstance(diff["key"], list):
                    key = values(diff["key"])
                else:
                    key = (f'<span title="A 主键">{values(diff["key"]["left"])}</span> → '
                           f'<span title="B 主键">{values(diff["key"]["right"])}</span>')
                lines.append(
                    f'<tr class="{"type-shift" if diff["type_change"] else ""}">'
                    f'<td>{diff["position"] + 1}</td><td class="mono">{key}</td>'
                    f'<td>{safe(diff["column"])}</td>'
                    f'<td>{typed(diff["a"])}<small class="kind">{safe(diff["kind_a"])}</small></td>'
                    f'<td>{typed(diff["b"])}<small class="kind">{safe(diff["kind_b"])}</small></td>'
                    f'<td>{badge}</td></tr>')
            body = ('<div class="scroll"><table class="compare-table"><thead><tr>'
                    '<th>行位置</th><th>主键</th><th>列</th><th>A 值</th><th>B 值</th><th>判定</th>'
                    '</tr></thead><tbody>' + "".join(lines) + '</tbody></table></div>')
        if table_report["key_positions_match"]:
            position_note = "行位置与有序主键序列一致"
        elif table_report["positions_match"]:
            position_note = "行数一致，按行位置对齐；主键编码或类型有变化"
        else:
            position_note = "行数不一致，已按行位置配对"
        sections.append(
            f'<section class="paper compare-table-card"><div class="panel-title">'
            f'<h2>表 {safe(table_report["table"])}</h2>'
            f'<span class="tag">A {table_report["rows_a"]} 行 / B {table_report["rows_b"]} 行 · {position_note}'
            f' · 类型差异 {table_report["type_change_count"]}</span></div>{body}</section>')
    summary = (f'类型差异 <b>{report["type_change_count"]}</b> 处 · 同类型值差异 '
               f'<b>{report["value_change_count"]}</b> 处 · 行位置一致：'
               f'<b>{"是" if report["positions_match"] else "否"}</b> · '
               f'内容摘要相同：<b>{"是" if report["content_equivalent"] else "否"}</b>')
    return page("compare", "身份对照", verdict_class=badge_class, verdict_text=badge_text,
                verdict_label=safe(report["verdict_label"]), summary=summary,
                sides=cards, sections="".join(sections),
                a_id=report["a"]["id"], b_id=report["b"]["id"])
