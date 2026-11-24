from .render import STATE_LABELS, page, safe

ROLE_LABELS = {"base": "祖本", "left": "左支", "right": "右支"}
REASON_LABELS = {"open_plan": "开放计划", "sealed_result": "封存结果",
                 "certificate_review": "凭证复核"}


def _references(entry):
    if not entry["references"]:
        return '<span class="null">无引用</span>'
    return "、".join(
        f'<a href="/plans/{safe(ref["plan"])}">{safe(ref["plan"][:10])}</a>'
        f'<small>（{ROLE_LABELS[ref["role"]]} · {STATE_LABELS[ref["state"]]}'
        f'{" · 已封存" if ref["sealed"] else ""}）</small>'
        for ref in entry["references"])


def _rows(entries, reasons=False):
    cells = "".join(
        f'<tr><th><a href="/images/{safe(e["id"])}">{safe(e["label"])}</a></th>'
        f'<td><code>{safe(e["id"][:14])}</code></td><td>{e["size"]:,} B</td>'
        + (f'<td>{_reasons(e["reasons"])}</td>' if reasons else "")
        + f'<td>{_references(e)}</td></tr>' for e in entries)
    return cells


def _reasons(reasons):
    if not reasons:
        return '<span class="null">—</span>'
    return "、".join(REASON_LABELS.get(reason, reason) for reason in reasons)


def reclaim_page(report, history):
    counts = report["counts"]
    retained = _rows(report["retained"], reasons=True) or '<tr><td colspan="5" class="muted">没有需要保留的镜像。</td></tr>'
    reclaimable = _rows(report["reclaimable"]) or '<tr><td colspan="4" class="muted">没有可回收的镜像。</td></tr>'
    done = _rows(report["reclaimed"]) or '<tr><td colspan="4" class="muted">尚未回收任何镜像。</td></tr>'
    if report["reclaimable"]:
        form = (f'<form class="confirm" method="post" action="/reclaims">'
                f'<input type="hidden" name="digest" value="{safe(report["digest"])}">'
                f'<p>将清空 {counts["reclaimable"]} 份镜像的原始内容，预计释放 '
                f'{report["freed_bytes"]:,} B。计划、裁决、重演与凭证记录保持可读。</p>'
                f'<button class="primary" type="submit">确认回收</button></form>')
    else:
        form = '<p class="muted">当前没有可回收的原始镜像。</p>'
    history_rows = "".join(
        f'<tr><th><a href="/api/reclaims/{safe(h["digest"])}"><code>{safe(h["digest"][:14])}</code></a></th>'
        f'<td>{safe(h["executed_at"])}</td><td>{h["reclaimed"]} 份</td>'
        f'<td>{h["freed_bytes"]:,} B</td></tr>' for h in history)
    return page("reclaim", "原始镜像回收", digest=safe(report["digest"]),
                retained_count=counts["retained"], reclaimable_count=counts["reclaimable"],
                reclaimed_count=counts["reclaimed"], freed=f'{report["freed_bytes"]:,}',
                retained_bytes=f'{report["retained_bytes"]:,}',
                retained=retained, reclaimable=reclaimable, reclaimed=done, form=form,
                history=history_rows or '<tr><td colspan="4" class="muted">尚无回收记录。</td></tr>')
