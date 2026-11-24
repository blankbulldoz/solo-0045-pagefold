from .render import ROLE_LABELS, STATE_LABELS, page, safe, values

VERDICTS = {"compatible": ("需一起考虑 · 当前可同采纳", "auto"),
            "pending": ("需一起考虑 · 有待裁决项", ""),
            "clash": ("必须改选", "conflict")}
NOTES = {"automatic": "自动采纳，不可改选", "pending": "待裁决",
         "keep": "当前选择可保留，但须配合他项改选", "change": "当前选择不可行，必须改选", "linked": ""}
MAX_CHIPS = 60


def item_label(item):
    return f'{safe(item["table"])} <span class="muted">[{values(item["key"])}]</span>'


def item_row(item):
    source = "自动" if item["automatic"] else "冲突"
    choice = ROLE_LABELS.get(item["choice"]) or "待裁决"
    return (f'<tr><td>{safe(item["table"])}</td><td>{values(item["key"])}</td>'
            f'<td>{source}</td><td>{choice}</td><td>{safe(NOTES[item["note"]])}</td></tr>')


def edge_line(edge, labels):
    columns = "、".join(edge["columns"])
    references = f'{edge["ref_table"]}（{"、".join(edge["ref_columns"])}）'
    roles = "、".join(ROLE_LABELS[role] for role in edge["roles"])
    if edge["violated"]:
        flag = ' <span class="tag conflict">当前违例</span>'
    elif edge["active"]:
        flag = ' <span class="tag auto">当前生效</span>'
    else:
        flag = ""
    return (f'<li>{labels[edge["from"]]} → {labels[edge["to"]]}：'
            f'列 {safe(columns)} 引用 {safe(references)}，值 {values(edge["values"])}，'
            f'约束 {edge["constraint"]}，{roles}激活。{flag}</li>')


def group_block(number, group):
    label, style = VERDICTS[group["verdict"]]
    labels = {item["id"]: item_label(item) for item in group["items"]}
    rows = "".join(item_row(item) for item in group["items"])
    edges = "".join(edge_line(edge, labels) for edge in group["edges"])
    detail = [f'<ul class="coadopt-edges">{edges}</ul>']
    if group["verdict"] == "clash":
        detail.append('<p>当前候选集合在此组不可同时采纳。</p>')
        if group["repairable"]:
            fixes = "".join(
                f'<li>改 {fix["changes"]} 项：' +
                "、".join(f'{labels[item_id]} → {ROLE_LABELS[role]}'
                          for item_id, role in fix["items"].items()) + '</li>'
                for fix in group["fixes"])
            detail.append(f'<p>可行的改选方案：</p><ul class="coadopt-fixes">{fixes}</ul>')
        elif group["repairable"] is False:
            detail.append('<p>该组的自动采纳结果互相冲突，任何裁决组合都无法修复，需要调整输入镜像。</p>')
        else:
            detail.append('<p>组合过多，未逐一枚举；请以重演验证。</p>')
    for item_id, roles in group["options"].items():
        choices = "、".join(ROLE_LABELS[role] for role in roles)
        detail.append(f'<p>待裁决项 {labels[item_id]} 可保持联动的选择：{safe(choices)}。</p>')
    return (f'<section class="paper"><div class="panel-title"><h2>第 {number} 组 · {label}</h2>'
            f'<span class="tag {style}">{len(group["items"])} 项</span></div>'
            f'<div class="scroll"><table><thead><tr><th>表</th><th>主键</th><th>来源</th>'
            f'<th>当前选择</th><th>说明</th></tr></thead><tbody>{rows}</tbody></table></div>'
            f'<div class="coadopt-body">{"".join(detail)}</div></section>')


def coadopt_page(report):
    summary = report["summary"]
    distribution = (f'{summary["independent"]} 项可独立操作 · {summary["linked"]} 项需一起考虑 · '
                    f'{summary["rechoose"]} 项必须改选')
    if summary["pending"]:
        distribution += f' · {summary["pending"]} 项待裁决'
    groups = "".join(group_block(number + 1, group) for number, group in enumerate(report["groups"]))
    shown = report["independent"][:MAX_CHIPS]
    chips = "".join(f'<span class="chip">{item_label(item)}</span>' for item in shown)
    if len(report["independent"]) > len(shown):
        chips += f'<span class="chip">等 {len(report["independent"])} 项</span>'
    if not chips:
        chips = '<span class="muted">没有可独立操作的项。</span>'
    return page("coadopt", "联动采纳分析", plan=report["plan"], short=report["plan"][:12],
                revision=report["revision"], state=STATE_LABELS[report["state"]],
                count=summary["items"], distribution=distribution, groups=groups,
                independent_count=summary["independent"], chips=chips)
