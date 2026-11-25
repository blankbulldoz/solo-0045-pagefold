from urllib.parse import urlencode

from .catalog import Catalog, Table
from .render import option, page, safe, typed


def image_options(images):
    return option("", "选择已登记镜像") + "".join(
        option(image["id"], f'{image["label"]} · {image["id"][:10]}') for image in images)


def image_links(images):
    return "".join(f'<li><a href="/images/{i["id"]}">{safe(i["label"])}</a>'
                   f'<code>{i["id"][:12]}</code><small>{i["size"]:,} B</small></li>' for i in images)


def image_page(image):
    catalog = Catalog.from_record(image["catalog"])
    tables = []
    for table in catalog.tables:
        url = f'/images/{image["id"]}/rows?' + urlencode({"table": table.name})
        references = "、".join(foreign.label() for foreign in table.foreign_keys) or "—"
        tables.append(f'<tr><th><a href="{safe(url)}">{safe(table.name)}</a></th>'
                      f'<td>{safe(" · ".join(table.key_names))}</td><td>{safe(references)}</td>'
                      f'<td>{table.rows}</td></tr>')
    return page("image", image["label"], label=safe(image["label"]), image_id=image["id"],
                signature=catalog.signature, tables="".join(tables), size=f'{image["size"]:,}')


def rows_page(image, result):
    table = Table.from_record(result["table"])
    offset, total = result["offset"], result["total"]
    columns = "".join(f'<th scope="col">{safe(column.name)}</th>' for column in table.columns)
    rows = "".join('<tr>' + "".join(f'<td>{typed(v)}</td>' for v in row["values"]) + '</tr>'
                   for row in result["rows"])
    nav = []
    for label, target, exists in [("上一页", max(0, offset - 50), offset > 0),
                                  ("下一页", offset + 50, offset + 50 < total)]:
        if exists:
            params = urlencode({"table": table.name, "offset": target})
            nav.append(f'<a href="?{safe(params)}">{label}</a>')
    return page("rows", table.name, name=safe(table.name), label=safe(image["label"]),
                image_id=image["id"], columns=columns, rows=rows, nav="".join(nav), total=total,
                window=f"{offset + 1}–{min(offset + 50, total)}" if rows else "当前页无行")
