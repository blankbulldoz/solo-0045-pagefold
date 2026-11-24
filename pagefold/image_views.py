from urllib.parse import urlencode

from .render import option, page, safe, typed


def image_options(images):
    return option("", "选择已登记镜像") + "".join(
        option(image["id"], f'{image["label"]} · {image["id"][:10]}') for image in images)


def image_links(images):
    return "".join(f'<li><a href="/images/{i["id"]}">{safe(i["label"])}</a>'
                   f'<code>{i["id"][:12]}</code><small>{i["size"]:,} B'
                   f'{" · 原始内容已回收" if i["reclaimed"] else ""}</small></li>' for i in images)


def image_page(image):
    tables = []
    for table in image["catalog"]["tables"]:
        if image["reclaimed"]:
            tables.append(f'<tr><th>{safe(table["name"])}</th>'
                          f'<td>{safe(" · ".join(table["key"]))}</td><td>—</td><td>{table["rows"]}</td></tr>')
            continue
        url = f'/images/{image["id"]}/rows?' + urlencode({"table": table["name"]})
        references = "、".join(f"{row[3]} → {row[2]}.{row[4] or '主键'}" for row in table["foreign_keys"]) or "—"
        tables.append(f'<tr><th><a href="{safe(url)}">{safe(table["name"])}</a></th>'
                      f'<td>{safe(" · ".join(table["key"]))}</td><td>{safe(references)}</td>'
                      f'<td>{table["rows"]}</td></tr>')
    notice = ('<p class="muted reclaim-notice">原始内容已回收，行数据不可读取；目录与来源标识仍然保留。</p>'
              if image["reclaimed"] else "")
    return page("image", image["label"], label=safe(image["label"]), image_id=image["id"],
                signature=image["catalog"]["signature"], tables="".join(tables), size=f'{image["size"]:,}',
                notice=notice)


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
