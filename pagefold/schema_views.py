from .render import ROLE_LABELS, page, safe
from .schema_diff import FINDING_KIND, FINDING_LABELS


def evidence_block(evidence, links):
    columns = "".join(f"<li>{safe(column)}</li>" for column in evidence["columns"]) or "<li class='muted'>—</li>"
    indexes = "".join(f"<li>{safe(index)}</li>" for index in evidence["indexes"]) or "<li class='muted'>无显式索引</li>"
    head = ROLE_LABELS[evidence["role"]]
    if evidence["present"] and evidence["role"] in links:
        head = f'<a href="/images/{safe(links[evidence["role"]])}">{head} 镜像目录 ↗</a>'
    if not evidence["present"]:
        head += ' <span class="tag absent">该库缺表</span>'
    return (f'<div class="evidence"><dt>{head}</dt>'
            f'<dd><b>列</b><ul class="structure-list">{columns}</ul>'
            f'<b>主键顺序</b><p><code>{safe(" · ".join(evidence["key"]) or "—")}</code></p>'
            f'<b>显式索引</b><ul class="structure-list">{indexes}</ul></dd></div>')


def finding_block(finding, links):
    subject = f'<code>{safe(finding["subject"])}</code> · ' if finding.get("subject") else ""
    affected = "、".join(ROLE_LABELS[role] for role in finding["roles"])
    evidence = "".join(evidence_block(finding["evidence"][role], links) for role in ROLE_LABELS)
    return (f'<article class="finding"><div class="finding-head">'
            f'<h3><span class="tag kind">{safe(FINDING_KIND[finding["kind"]])}</span> '
            f'{subject}<span>{safe(finding["label"])}</span></h3>'
            f'<p>{safe(finding["detail"])}<small>差异来源镜像：{safe(affected)}</small></p></div>'
            f'<dl class="evidence-grid">{evidence}</dl></article>')


def summary_chips(summary):
    return "".join(f'<span class="tag chip">{safe(FINDING_KIND[kind])} {summary[kind]} 项</span>'
                   for kind in FINDING_KIND if summary.get(kind))


def mismatch_page(report, links=None):
    links = links or {}
    roles = "".join(
        f'<div><dt>{safe(role_info["label"])} <small>（{safe(ROLE_LABELS[role])}）</small></dt>'
        f'<dd><code>{safe(role_info["signature"])}</code></dd></div>'
        for role, role_info in report["roles"].items())
    findings = "".join(finding_block(finding, links) for finding in report["findings"])
    return page("mismatch", "模式精确匹配未通过", roles=roles, count=report["count"],
                chips=summary_chips(report["summary"]), findings=findings)
