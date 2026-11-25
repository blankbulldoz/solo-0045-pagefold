from .render import ROLE_LABELS, page, safe


def group_links(groups):
    return "".join(
        f'<a class="plan-link" href="/groups/{g["id"]}">'
        f'<span>{safe(g["labels"]["base"])} <b>＋</b> {safe(g["labels"]["left"])} '
        f'<b>＋</b> {safe(g["labels"]["right"])}</span>'
        f'<small>{g["id"][:12]} · {safe(g["created_at"][:19].replace("T", " "))}</small></a>'
        for g in groups)


def group_page(group):
    cards = []
    for role, label in ROLE_LABELS.items():
        image = group["images"][role]
        tables = "、".join(safe(t["name"]) for t in image["catalog"]["tables"])
        cards.append(
            f'<a class="source-card" href="/images/{image["id"]}"><small>{label}</small>'
            f'<b>{safe(image["label"])}</b><code>{image["id"][:16]}</code>'
            f'<em>{image["size"]:,} B · 表 {tables}</em></a>')
    form = (f'<form method="post" action="/plans"><input type="hidden" name="group" value="{group["id"]}">'
            f'<button class="primary" type="submit">承接该组建立计划</button></form>')
    return page("group", "镜像组", group=group["id"], short=group["id"][:12],
                created=safe(group["created_at"][:19].replace("T", " ")),
                cards="".join(cards), form=form)
