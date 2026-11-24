from .attempts import compare_attempts, list_attempts
from .compare import resolve_pair, wants_verify
from .errors import Problem
from .plans import read_plan
from .render import ROLE_LABELS, page, safe, values

EVIDENCE_LABELS = {
    "recorded": ("留存选择", "b-recorded"),
    "candidate": ("候选反查来源", "b-proven"),
    "candidate_unmatched": ("候选实证·来源未匹配", "b-warn"),
    "current": ("当前版推断", "b-infer"),
    "missing": ("选择缺失", "b-missing"),
    "outside": ("计划外行", "b-muted"),
}

ROW_EVIDENCE_LABELS = {
    "proven": ("候选实证", "b-proven"),
    "inferred": ("待重演推断", "b-infer"),
    "unknown": ("无法还原", "b-missing"),
}

KIND_LABELS = {"modified": "修改", "added": "新增", "removed": "删除/缺席",
               "same": "相同", "undetermined": "无法判定"}

STATE_LABELS = {
    "same_candidate": "同一候选",
    "candidate_diff": "成功 / 成功",
    "passed_blocked": "成功 / 阻断",
    "both_blocked": "阻断 / 阻断",
    "incomplete": "记录不完整",
}


def badge(text, cls="b-muted"):
    return f'<span class="badge {cls}">{safe(text)}</span>'


def choice_line(cell):
    roles = cell.get("choice_roles") or []
    evidence = cell.get("choice_evidence")
    if evidence == "outside":
        return '<span class="cmp-choice muted">计划外实际行</span>'
    if cell.get("choice"):
        text = ROLE_LABELS.get(cell["choice"], cell["choice"])
        if cell.get("source_id"):
            choice = (f'<a class="choice-source" href="/images/{safe(cell["source_id"])}" '
                      f'title="查看该选择对应的来源镜像">{safe(text)}来源 ↗</a>')
        else:
            choice = f"<b>{safe(text)}</b>"
    elif len(roles) > 1:
        text = " / ".join(ROLE_LABELS.get(r, r) for r in roles) + "（同值）"
        choice = f"<b>{safe(text)}</b>"
    else:
        choice = '<b class="muted">未确定当时选择</b>'
    label, cls = EVIDENCE_LABELS.get(evidence, (evidence, "b-muted"))
    return f'<span class="cmp-choice">{choice}{badge(label, cls)}</span>'


def cell_html(cell):
    evidence = cell.get("row_evidence")
    if evidence == "unknown":
        value = '<span class="null">无依据，不能还原</span>'
    else:
        value = values(cell.get("row"))
    row_label, row_cls = ROW_EVIDENCE_LABELS.get(evidence, ("未知", "b-muted"))
    failed = ' <span class="badge b-block">触发约束失败</span>' if cell.get("failed_operation") else ""
    amended = ""
    if cell.get("amended") is True:
        amended = '<span class="badge b-muted">相对祖本已改</span>'
    return (f'<div class="cmp-cell ev-{safe(evidence)}">{choice_line(cell)}'
            f'<div class="cmp-value">{value}</div>'
            f'<div class="cmp-badges">{badge(row_label, row_cls)}{amended}{failed}</div></div>')


def row_note(row):
    notes = []
    if row["proven_change"] is True:
        notes.append('<span class="note-proven">两侧候选实物均可重读，差异已证明。</span>')
    elif row["proven_change"] is False:
        notes.append('<span class="muted">两侧候选实际值相同。</span>')
    elif row["kind"] == "same":
        notes.append('<span class="note-infer">一侧候选实际值与另一侧的推断目标值相同；'
                     "但另一侧没有候选，仍须重演通过后才能证明。</span>")
    else:
        notes.append('<span class="note-infer">至少一侧没有候选，此处差异仍是待重演的推断。</span>')
    if row["choice_changed"]:
        notes.append(f'<span class="note-choice">裁决角色发生变化：'
                     f'{safe(ROLE_LABELS.get(row["a"]["choice"], "?"))} → '
                     f'{safe(ROLE_LABELS.get(row["b"]["choice"], "?"))}。</span>')
    if row["blocker"]:
        ctx = row["blocker"].get("context") or {}
        notes.append('<span class="note-block">这一操作在重演时触发约束并中断，'
                     '该行未能形成候选。</span>')
    return "<br>".join(notes)


def _row_tr(row):
    key = values(row["key"])
    item_mark = "" if row["in_plan"] else badge("计划外", "b-muted")
    blocker_cls = " row-blocker" if row["blocker"] else ""
    kind_cls = "kind-" + row["kind"]
    return (
        f'<tr class="diff-{safe(row["kind"])}{blocker_cls}">'
        f'<td class="cell-id"><h3>{safe(row["table"])}</h3>'
        f'<code>{key}</code>{item_mark}</td>'
        f'<td>{cell_html(row["a"])}</td><td>{cell_html(row["b"])}</td>'
        f'<td><span class="kind {kind_cls}">{KIND_LABELS[row["kind"]]}</span></td>'
        f'<td class="cell-note">{row_note(row)}</td></tr>')


def diff_rows_html(comparison):
    rows = comparison["changes"]
    if not rows:
        return ('<tr><td colspan="5" class="empty">两版之间没有需要列出的行差异、'
                '选择变化或阻断操作。</td></tr>')
    return "".join(_row_tr(row) for row in rows)


def unchanged_html(comparison):
    same_rows = [row for row in comparison["rows"]
                 if row["kind"] == "same" and not row["choice_changed"] and row["blocker"] is None]
    if not same_rows:
        return ""
    body = "".join(_row_tr(row) for row in same_rows)
    a_rev, b_rev = comparison["a_revision"], comparison["b_revision"]
    return (
        f'<details class="unchanged"><summary>查看其余 {len(same_rows)} 个未变化计划项'
        "（同样关联当时选择与候选）</summary>"
        '<div class="scroll"><table class="compare-table">'
        f"<thead><tr><th>表 / 主键</th><th>第 {a_rev} 版</th><th>第 {b_rev} 版</th>"
        "<th>变化</th><th>证据说明</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></details>")


def constraint_kind(detail):
    upper = (detail or "").upper()
    if "UNIQUE" in upper:
        return "唯一约束"
    if "FOREIGN KEY" in upper:
        return "外键约束"
    if "NOT NULL" in upper:
        return "非空约束"
    if "CHECK" in upper:
        return "检查约束"
    if "PRIMARY KEY" in upper:
        return "主键约束"
    return "数据库约束"


def side_card(side, other_rev):
    rev = side["revision"]
    if side["passed"]:
        status = badge("通过 · 有候选", "b-proven")
        digest = (f'<code class="digest" title="{safe(side["candidate_digest"] or "")}">'
                  f'{safe((side["candidate_digest"] or "")[:18])}…</code>'
                  if side["candidate_digest"] else '<span class="null">摘要缺失</span>')
        candidate_line = f'<dt>候选摘要</dt><dd>{digest} <small>{side.get("candidate_size") or "?"} B</small></dd>'
    else:
        status = badge("阻断 · 无候选", "b-block")
        problem = side.get("problem") or {}
        kind = constraint_kind(problem.get("detail")) if problem.get("code") == "constraint" else "重演错误"
        candidate_line = (f'<dt>阻断原因</dt><dd><span class="note-block">{safe(kind)}</span>'
                           f'<code class="problem-detail">{safe(problem.get("detail", ""))}</code></dd>')
    snapshot_line = ("选择快照已留存" if side["choices_recorded"]
                     else "旧记录未留存选择，需反查或推断")
    reproduction = reproduction_html(side.get("reproduction") or {})
    return (f'<section class="paper side-card"><div class="panel-title">'
            f'<h2>第 {rev} 版重演</h2>{status}</div>'
            f'<dl class="side-meta"><div><dt>应用行数</dt><dd>{safe(side.get("changed"))}</dd></div>'
            f'<div>{candidate_line}</div>'
            f'<div><dt>当时选择</dt><dd>{safe(snapshot_line)}</dd></div>'
            f'</dl>{reproduction}</section>')


def reproduction_html(repro):
    if not repro:
        return ""
    if not repro.get("supported"):
        return f'<p class="repro unsupported">无法离线复核：{safe(repro.get("note", "缺少选择快照。"))}</p>'
    if "reproduced" not in repro:
        return ""
    if repro["reproduced"] and repro.get("replayed") == "blocked":
        return ('<p class="repro ok">✓ 离线重放再次卡在同一约束，阻断点可重现'
                "（仅内存复核，不产生正式记录）。</p>")
    if repro["reproduced"]:
        digest_short = safe((repro.get("candidate_digest") or "")[:18])
        return ('<p class="repro ok">✓ 离线重放得到相同候选摘要，来源与操作可重现。'
                f'<code class="digest">{digest_short}…</code></p>')
    problem = repro.get("problem") or {}
    detail = problem.get("detail") or repro.get("note") or "摘要不一致"
    return f'<p class="repro bad">离线重放未能重现原记录：{safe(detail)}</p>'


def banner_html(comparison, verify):
    state = comparison["state"]
    a, b = comparison["a"], comparison["b"]
    digest_a = a.get("candidate_digest") or ""
    digest_b = b.get("candidate_digest") or ""
    if state == "same_candidate":
        cls, title = "banner-proven", "已证明：两版返回同一候选"
        text = (f'两版留存候选的 SHA-256 完全一致（<code>{safe(digest_a[:20])}…</code>），'
                "字节相同，因此不存在可证明的行差异。相同操作集合会重放出同一候选；"
                "而对同一版本再次提交重演时，系统直接返回已留存的同一条记录和同一个候选。")
    elif state == "candidate_diff":
        cls, title = "banner-proven", "已证明：两个候选实物逐行不同"
        text = ("两次重演都通过并留存了候选字节。下表的“候选实证”值直接打开候选 SQLite 重读，"
                "行差异由候选实物证明；选择来自重演当时留存的快照，旧记录则由候选反查来源角色。")
    elif state == "passed_blocked":
        passed_rev = a["revision"] if a["passed"] else b["revision"]
        blocked = b if a["passed"] else a
        problem = blocked.get("problem") or {}
        kind = constraint_kind(problem.get("detail")) if problem.get("code") == "constraint" else "错误"
        cls, title = "banner-infer", f"第 {passed_rev} 版通过留证，另一版卡在{kind}"
        text = ("通过侧有候选字节，实际行可证明；阻断侧没有候选，阻断原因是已记录的事实，"
                "但下表中该侧的目标行只是依据当时选择整理的<b>待重演推断</b>，"
                "并非已经写入候选的结果。修正裁决后重新重演通过，这些行才转为证明。")
    elif state == "both_blocked":
        cls, title = "banner-infer", "两版均阻断，均无候选"
        text = ("两条记录都停在约束或重放错误上：阻断事实可回查，目标行只能依据选择快照或"
                "当前裁决推断，须待重演通过后才能证明。")
    else:
        cls, title = "banner-muted", "重演记录不完整"
        text = "至少一侧缺少可比较的证据，请确认两版都已执行过重演。"
    verify_hint = "" if verify else (
        f'<a class="verify-link" href="?a={a["revision"]}&amp;b={b["revision"]}&amp;verify=1">'
        "运行可重现性离线复核 →</a>")
    return f'<div class="cmp-banner {cls}"><h2>{title}</h2><p>{text}</p>{verify_hint}</div>'


def evidence_guide_html():
    items = [
        ("b-proven", "候选实证 / 候选反查", "直接读取留存候选中的实际行，这部分已经证明；旧通过记录即使没有选择快照，也能用候选行与祖、左、右三方值比对，证明来源角色。"),
        ("b-recorded", "留存选择", "重演当时写入 attempt 记录的角色快照，是“当时选了什么”的直接证据。"),
        ("b-infer", "当前版推断", "旧的受阻记录既无候选也无选择快照时，按计划当前裁决推算的目标行；它不是历史事实，仍只是待重演的推断。"),
        ("b-missing", "选择缺失", "旧数据没有留存选择、当前也未裁决该条目时明确标缺，页面不会虚构当时的选择。"),
    ]
    body = "".join(f'<li>{badge(label, cls)}<span>{safe(text)}</span></li>' for cls, label, text in items)
    intro = ('<p class="guide-intro">本页严格区分：从留存候选重读的行与已记录的阻断原因'
             '<b>已经证明</b>；没有候选的一侧，其目标行<b>仍只是待重演的推断</b>。'
             '对于旧数据缺失选择信息的情况，按下列级别如实呈现，绝不补造历史选择。</p>')
    return ('<section class="paper evidence-guide"><div class="panel-title">'
            '<h2>证据级别与旧数据缺失选择的呈现方式</h2></div>'
            f'{intro}<ul>{body}</ul></section>')


def sources_html(comparison):
    source_links = []
    for role in ("base", "left", "right"):
        source = comparison["sources"][role]
        source_links.append(
            f'<a href="/images/{source["id"]}"><small>{ROLE_LABELS[role]}来源</small>'
            f'<b>{safe(source["label"])}</b><code>{safe(source["id"][:18])}…</code></a>')
    summary = comparison["summary"]
    stats = (f'差异行 {summary["changed"]} · 候选证明差异 {summary["proven_changes"]} · '
             f'推断行 {summary["inferred_rows"]} · 缺失选择 {summary["missing_choices"]}')
    return (
        '<section class="paper source-panel"><div class="panel-title"><h2>可重现的来源</h2>'
        f'<span class="tag">{safe(stats)}</span></div>'
        f'<nav class="sources">{ "".join(source_links) }</nav>'
        f'<p class="repro-method">重现路径：从祖本镜像字节出发，'
        '<code>amendments(items, choices)</code> 生成逐行操作，再由 '
        '<code>rebuild(base, catalog, operations)</code> 重建并校验。'
        f'模式摘要 <code>{safe(comparison["schema_signature"][:20])}…</code>。'
        "镜像按 SHA-256 寻址且不可变，因此来源、选择快照与候选摘要齐备时可离线复核；"
        "勾选“离线复核可重现性”会在内存中重放一次，不改变计划与正式重演历史。</p></section>")


def selector_html(plan_id, attempts, a, b, verify):
    def options(selected_rev):
        out = []
        for attempt in attempts:
            rev = attempt["revision"]
            status = "通过" if attempt.get("status") == "passed" else "受阻"
            out.append(f'<option value="{rev}"{" selected" if rev == selected_rev else ""}>'
                       f'第 {rev} 版 · {status} · 改 {attempt.get("changed")} 行</option>')
        return "".join(out) or '<option value="">暂无重演记录</option>'
    disabled = " disabled" if not attempts else ""
    checked = " checked" if verify else ""
    return (
        f'<form class="compare-form" method="get" action="/plans/{plan_id}/compare">'
        f'<label>基准版<select name="a"{disabled}>{options(a)}</select></label>'
        f'<span class="vs">对照</span>'
        f'<label>新版<select name="b"{disabled}>{options(b)}</select></label>'
        f'<label class="verify-check"><input type="checkbox" name="verify" value="1"{checked}> 离线复核可重现性</label>'
        f'<button class="primary" type="submit"{disabled}>比较两版</button></form>')


def compare_page(plan, attempts, comparison, a, b, verify):
    plan_id = plan["id"]
    if comparison is None:
        banner = ('<div class="cmp-banner banner-muted"><h2>还没有可比较的重演记录</h2>'
                  "<p>先在计划页完成裁决并重演；产生两条记录后即可比较两版“实际何处变了”。"
                  "只有一条记录时也可以用同一版本自比，查看“相同操作返回同一候选”。</p></div>")
        rows = ""
        unchanged = ""
        side_cards = ""
        row_count = "0"
        state_label = "无记录"
    else:
        banner = banner_html(comparison, verify)
        rows = diff_rows_html(comparison)
        unchanged = unchanged_html(comparison)
        side_cards = ('<div class="cmp-side-grid">'
                      + side_card(comparison["a"], b) + side_card(comparison["b"], a) + "</div>")
        row_count = str(len(comparison["changes"]))
        state_label = STATE_LABELS.get(comparison["state"], "比较")
    body_extras = evidence_guide_html()
    sources = sources_html(comparison) if comparison else ""
    return page(
        "compare", "两版重演比较", plan=plan_id, short=plan_id[:12],
        a_rev=a if a is not None else "—", b_rev=b if b is not None else "—",
        state_label=state_label,
        picker=selector_html(plan_id, attempts, a, b, verify),
        banner=banner, rows=rows, unchanged=unchanged, row_count=row_count,
        side_cards=side_cards, guide=body_extras, sources=sources)


def comparison_workspace(db, plan_id, query, path_pair=None):
    """HTML 工作区入口：读取计划、历史并渲染比较页。"""
    plan = read_plan(db, plan_id)
    attempts = list_attempts(db, plan_id)
    revisions = [attempt["revision"] for attempt in attempts]
    if path_pair is not None:
        a, b = path_pair
    else:
        a, b = resolve_pair(query, revisions)
    verify = wants_verify(query)
    comparison = compare_attempts(db, plan_id, a, b, verify=verify) if a is not None else None
    return compare_page(plan, attempts, comparison, a, b, verify)
