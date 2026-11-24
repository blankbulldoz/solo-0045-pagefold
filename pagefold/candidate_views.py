from urllib.parse import urlencode

from .render import ROLE_LABELS, STATE_LABELS, fragment, page, safe, typed, values


def marker(plan, revision):
    if plan["revision"] == revision:
        return "当前版"
    return f"历史版 · 当前计划为第 {plan['revision']} 版"


def choice_label(item):
    label = ROLE_LABELS.get(item["choice"], "待裁决")
    return ("自动 · " if item["automatic"] else "") + label


def passed_sections(plan_id, plan, result, overview):
    meta = ('<section class="paper"><div class="panel-title"><h2>候选标识</h2></div><dl class="meta">'
            f'<dt>候选摘要</dt><dd><code>{result["candidate_digest"]}</code></dd>'
            f'<dt>候选大小</dt><dd>{result["size"]:,} B</dd>'
            f'<dt>应用行数</dt><dd>{result["changed"]}</dd>'
            f'<dt>版本位置</dt><dd>{safe(marker(plan, result["revision"]))}</dd>'
            f'<dt>计划状态</dt><dd>{safe(STATE_LABELS[plan["state"]])}</dd>'
            '</dl></section>')
    rows = []
    for table in overview["tables"]:
        url = f'/plans/{plan_id}/attempts/{result["revision"]}/rows?' + urlencode({"table": table["name"]})
        rows.append(f'<tr><th><a href="{safe(url)}">{safe(table["name"])}</a></th>'
                    f'<td>{safe(" · ".join(table["key"]))}</td><td>{table["rows"]}</td></tr>')
    structure = ('<section class="paper workspace-history"><div class="panel-title"><h2>关系结构</h2></div>'
                 '<div class="scroll"><table><thead><tr><th>表</th><th>主键顺序</th><th>行数</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div></section>')
    entries = "".join(fragment("synthesis", table=safe(item["table"]), key=values(item["key"]),
                               choice=choice_label(item),
                               badge="auto" if item["automatic"] else "conflict",
                               candidate=values(item["candidate"]),
                               **{role: values(item[role]) for role in ROLE_LABELS})
                      for item in overview["entries"])
    empty = '<div class="empty">三份镜像没有行差异。</div>'
    compare = ('<section class="paper workspace-history"><div class="panel-title"><h2>合成对照</h2>'
               f'<span class="tag">{len(overview["entries"])} 项</span></div>'
               f'<div class="entries">{entries or empty}</div></section>')
    return meta + structure + compare


def blocked_sections(result):
    problem = result["problem"]
    return ('<section class="paper"><div class="panel-title"><h2>受阻诊断</h2></div><dl class="meta">'
            f'<dt>问题代码</dt><dd><code>{safe(problem["code"])}</code></dd>'
            f'<dt>诊断</dt><dd>{safe(problem["detail"])}</dd></dl>'
            '<div class="empty muted">该版重演受阻，没有产生候选；可裁决的来源行请回到计划页处理。</div></section>')


def candidate_page(plan_id, overview):
    plan, result = overview["plan"], overview["result"]
    revision = result["revision"]
    if result["status"] == "passed":
        sections, tone, status = passed_sections(plan_id, plan, result, overview), "auto", "通过"
    else:
        sections, tone, status = blocked_sections(result), "conflict", "受阻"
    return page("candidate", f"第 {revision} 版候选", plan=plan_id, short=plan_id[:12],
                revision=revision, marker=safe(marker(plan, revision)),
                tone=tone, status=status, sections=sections)


def candidate_rows_page(plan_id, revision, result):
    table = result["table"]
    offset, total = result["offset"], result["total"]
    columns = "".join(f'<th scope="col">{safe(col)}</th>' for col in table["columns"])
    rows = "".join("<tr>" + "".join(f"<td>{typed(value)}</td>" for value in row["values"]) + "</tr>"
                   for row in result["rows"])
    nav = []
    for label, target, exists in [("上一页", max(0, offset - 50), offset > 0),
                                  ("下一页", offset + 50, offset + 50 < total)]:
        if exists:
            params = urlencode({"table": table["name"], "offset": target})
            nav.append(f'<a href="?{safe(params)}">{label}</a>')
    return page("candidate_rows", table["name"], plan=plan_id, revision=revision,
                name=safe(table["name"]), columns=columns, rows=rows, nav="".join(nav),
                total=total, window=f"{offset + 1}–{min(offset + 50, total)}" if rows else "当前页无行")
