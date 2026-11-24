from urllib.parse import urlencode

from .render import option, page, safe, typed


def image_options(images):
    return option("", "选择已登记镜像") + "".join(
        option(image["id"], f'{image["label"]} · {image["id"][:10]}') for image in images)


def image_links(images):
    return "".join(f'<li><a href="/images/{i["id"]}">{safe(i["label"])}</a>'
                   f'<code>{i["id"][:12]}</code><small>{i["size"]:,} B</small></li>' for i in images)


def column_line(column):
    flags = []
    if column["notnull"]:
        flags.append("NOT NULL")
    if column["default"] is not None:
        flags.append(f"DEFAULT {column['default']}")
    return f'{safe(column["name"])} <em>{safe(column["type"] or "无类型声明")}</em>' + (
        f' <small>{" ".join(flags)}</small>' if flags else "")


def index_line(index):
    unique = "唯一索引" if index["unique"] else "普通索引"
    partial = " · 部分索引" if index["partial"] else ""
    origin = " · 约束自动索引" if index["origin"] == "u" else ""
    return (f'<details><summary><code>{safe(index["name"])}</code><small>{unique} · '
            f'{safe("、".join(index["columns"]))}{partial}{origin}</small></summary>'
            f'<pre>{safe(index["sql"])}</pre></details>')


def image_page(image):
    tables = []
    for table in image["catalog"]["tables"]:
        url = f'/images/{image["id"]}/rows?' + urlencode({"table": table["name"]})
        references = "、".join(f"{row[3]} → {row[2]}.{row[4] or '主键'}" for row in table["foreign_keys"]) or "—"
        columns = "".join(f"<li>{column_line(column)}</li>" for column in table["columns"])
        indexes = "".join(index_line(index) for index in table.get("indexes", []))
        indexes += "".join(index_line(index) for index in table.get("constraint_indexes", []))
        indexes = indexes or "<li class='muted'>无显式索引</li>"
        tables.append(f'<tr><th><a href="{safe(url)}">{safe(table["name"])}</a></th>'
                      f'<td><ul class="structure-list">{columns}</ul></td>'
                      f'<td><code>{safe(" · ".join(table["key"]))}</code></td>'
                      f'<td><ul class="structure-list index-list">{indexes}</ul></td>'
                      f'<td>{safe(references)}</td><td>{table["rows"]}</td></tr>')
    return page("image", image["label"], label=safe(image["label"]), image_id=image["id"],
                signature=image["catalog"]["signature"], tables="".join(tables), size=f'{image["size"]:,}')


def rows_page(image, result):
    table = result["table"]
    offset, total = result["offset"], result["total"]
    columns = "".join(f'<th scope="col">{safe(col["name"])}</th>' for col in table["columns"])
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
