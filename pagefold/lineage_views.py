from .render import ROLE_LABELS, page, safe
from .lineage import BASIS_LABELS, CONFIDENCE_LABELS, ROLE_LABELS as LINEAGE_ROLES

CONFIDENCE_CLASS = {"strong": "strong", "weak": "weak", "none": "none"}


def edge_items(edges):
    if not edges:
        return '<p class="muted">无此强度的来源边。</p>'
    rows = []
    for edge in edges:
        anchor = edge["plan_id"] or edge["source_image_id"] or "—"
        rows.append(
            f'<li class="lineage-edge {edge["confidence"]}">'
            f'<span class="tag conf-{edge["confidence"]}">{safe(CONFIDENCE_LABELS[edge["confidence"]])}证据</span>'
            f'<b>{safe(BASIS_LABELS[edge["basis"]])}</b>'
            f'<small>{safe(LINEAGE_ROLES.get(edge["role"], edge["role"]))} · {safe(anchor)}</small>'
            f'<em>{safe(edge["note"]) or "—"}</em></li>')
    return '<ul class="lineage-list">' + "".join(rows) + "</ul>"


def import_items(imports):
    if not imports:
        return '<p class="muted">暂无导入记录。</p>'
    return "<ul class='lineage-list'>" + "".join(
        f"<li><b>#{imp['id']}</b><small>{safe(imp['occurred_at'])}</small><em>{safe(imp['label'])}</em></li>"
        for imp in imports) + "</ul>"


def role_items(plan_roles):
    if not plan_roles:
        return '<p class="muted">该镜像尚未在正式计划中担任角色。</p>'
    return "<ul class='lineage-list'>" + "".join(
        f'<li><a href="/plans/{safe(r["plan_id"])}"><b>{safe(ROLE_LABELS[r["role"]])}</b></a>'
        f'<small>{safe(r["state"])} · 第 {r["revision"]} 版</small></li>'
        for r in plan_roles) + "</ul>"


def lineage_page(lineage):
    image_id = lineage["image_id"]
    strongest = CONFIDENCE_LABELS.get(lineage["strongest_confidence"], "无")
    return page(
        "lineage", "来源谱系", image_id=image_id, short=image_id[:12],
        strongest=safe(strongest),
        imports=import_items(lineage["imports"]),
        strong=edge_items(lineage["strong_evidence"]),
        weak=edge_items(lineage["weak_evidence"]),
        missing=edge_items(lineage["missing_evidence"]),
        roles=role_items(lineage["plan_roles"]),
        routes=str(lineage["derivation"]["route_count"]),
        plural="多条同等可信强路线" if lineage["derivation"]["plural_strong_routes"] else "单条强路线")
