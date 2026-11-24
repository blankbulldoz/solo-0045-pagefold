from urllib.parse import urlencode

from .codec import unpack
from .render import option, page, safe, typed
from .witness import DEFAULT_DEPTH, DEFAULT_FAN, top_witness_url, witness_url

WITNESS_TAIL = ("witness", "witnesses", "refs", "references", "neighbors",
                "graph", "lineage", "related", "trace")


def key_label(key_atoms, columns=None):
    parts = []
    for index, atom_part in enumerate(key_atoms):
        name = columns[index] if columns and index < len(columns) else f"键{index + 1}"
        parts.append(f'<span class="key-part"><dfn>{safe(name)}</dfn>{typed(atom_part)}</span>')
    return '<span class="key-chips">' + "".join(parts) + "</span>"


def row_table(definition, values_atoms, key_atoms=None, image_id=None, compact=False):
    head = "".join(f"<th>{safe(col)}</th>" for col in definition["columns"])
    cells = "".join(f"<td>{typed(value)}</td>" for value in values_atoms)
    link = ""
    if key_atoms is not None and image_id is not None:
        url = witness_url(image_id, definition["name"], key_atoms)
        link = f'<td class="row-link"><a href="{safe(url)}">见证 ↗</a></td>'
        head += "<th></th>"
    classes = "scroll compact" if compact else "scroll"
    return (f'<div class="{classes}"><table><thead><tr>{head}</tr></thead>'
            f"<tbody><tr>{cells}{link}</tr></tbody></table></div>")


def witness_table(definition, nodes, image_id):
    if not nodes:
        return '<p class="muted">没有实际引用行。</p>'
    head = "".join(f"<th>{safe(col)}</th>" for col in definition["columns"]) + "<th></th>"
    body = []
    for node in nodes:
        cells = "".join(f"<td>{typed(value)}</td>" for value in node["values"])
        url = witness_url(image_id, definition["name"], node["key"])
        body.append(f'<tr>{cells}<td class="row-link"><a href="{safe(url)}">重新定起点 ↗</a></td></tr>')
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def provenance(constraint):
    actions = []
    if constraint.get("on_delete") and constraint["on_delete"] != "NO ACTION":
        actions.append("ON DELETE " + constraint["on_delete"])
    if constraint.get("on_update") and constraint["on_update"] != "NO ACTION":
        actions.append("ON UPDATE " + constraint["on_update"])
    if constraint.get("match") and constraint["match"].upper() not in {"NONE", "SIMPLE"}:
        actions.append("MATCH " + constraint["match"])
    tail = f' · {safe(" · ".join(actions))}' if actions else ""
    return (
        '<p class="edge-source">边来源：'
        f'<code>外键约束 #{constraint["id"]}</code> '
        f'<code>({safe(", ".join(constraint["from_columns"]))})</code> → '
        f'<code>{safe(constraint["to_table"])}({safe(", ".join(constraint["to_columns"]))})</code>'
        f"{tail}</p>")


def upstream_block(group, tables, image_id):
    constraint = group["constraint"]
    definition = tables[group["table"]]
    heading = (f'<h3>{safe(group["table"])} 持有的外键 #{constraint["id"]} '
               f'<span class="tag auto">上游</span></h3>')
    body = [provenance(constraint)]
    if group["status"] == "contains_null":
        body.append(f'<p class="notice null-notice">∅ {safe(group["explain"])}</p>')
    elif group["status"] == "dangling":
        body.append(f'<p class="notice warn">⚠ {safe(group["explain"])}</p>')
    elif group.get("row") is not None:
        target_def = next(t for t in tables.values() if t["name"] == constraint["to_table"])
        body.append(row_table(target_def, group["row"], image_id=image_id,
                              key_atoms=_row_key(target_def, group["row"])))
    return f'<section class="paper edge-card">{heading}{"".join(body)}</section>'


def _row_key(definition, values_atoms):
    from .rows import key_atom
    return [key_atom(unpack(values_atoms[definition["columns"].index(col)]))
            for col in definition["key"]]


def downstream_block(group, tables, image_id):
    constraint = group["constraint"]
    definition = tables[group["table"]]
    more_badge = '<span class="tag conflict">未展全</span>' if group["truncated"] else ""
    heading = (f'<h3>{safe(group["table"])} 的入向引用 '
               f'<span class="tag">{group["total"]} 行</span>'
               f'{more_badge}</h3>')
    body = [provenance(constraint)]
    if group["total"] == 0:
        body.append('<p class="muted">没有行通过此外键引用当前主键。</p>')
    else:
        body.append(witness_table(definition, group["witnesses"], image_id))
    if group.get("null_like"):
        body.append(f'<p class="notice null-notice">∅ {safe(group["null_explain"])}</p>')
    if group["truncated"]:
        more_url = _more_url(group, image_id)
        body.append(f'<p class="notice warn">⚠ {safe(group["explain"])}'
                    f'<a href="{safe(more_url)}">用更大的 limit 重试</a>（上限 200）。</p>')
    return f'<section class="paper edge-card">{heading}{"".join(body)}</section>'


def _more_url(group, image_id):
    # 放大到能容纳全组（受接口上限约束），保留当前被引用起点的表与键。
    limit = min(200, max(DEFAULT_FAN * 2, group["total"]))
    return witness_url(image_id, group["source_table"], group["source_key"], limit=limit)


def cycle_block(result, tables):
    cycles = result["cycles"]
    if not cycles:
        return ""
    cards = []
    node_index = {node["id"]: node for node in result["nodes"]}
    for cycle in cycles:
        chips = []
        for nid in cycle["nodes"]:
            node = node_index.get(nid)
            if not node:
                continue
            url = node.get("url") or top_witness_url(
                result["image"]["id"], node["table"], node["key"])
            columns = tables[node["table"]]["key"] if node["table"] in tables else None
            chips.append(
                f'<a class="cycle-node" href="{safe(url)}">{safe(node["table"])} '
                f'{key_label(node["key"], columns)}</a>')
        cards.append(f'<li>{" ↻ ".join(chips)}</li>')
    return ('<section class="paper cycles"><div class="panel-title"><h2>外键循环</h2>'
            '<span class="tag conflict">有环</span></div>'
            f'<ul>{"".join(cards)}</ul>'
            '<p class="muted">沿子表外键指向父行的方向检测；环上的节点只展开一次。</p></section>')


def expansion_section(result, tables):
    root_id = result["root"]["id"]
    cards = []
    for node in result["nodes"]:
        if node["id"] == root_id or not node["expanded"]:
            continue
        groups = result["groups_by_node"].get(node["id"], {})
        key_columns = tables[node["table"]]["key"] if node["table"] in tables else None
        up = []
        for group in groups.get("upstream", []):
            c = group["constraint"]
            if group["status"] == "contains_null":
                up.append(f'<li><code>{safe(group["table"])}.({safe(",".join(c["from_columns"]))})'
                          f'</code> <span class="null">NULL 列 '
                          f'{safe("、".join(group["null_columns"]))}，无引用</span></li>')
            elif group.get("target"):
                target = next((n for n in result["nodes"] if n["id"] == group["target"]), None)
                if target:
                    target_cols = tables[target["table"]]["key"] if target["table"] in tables else None
                    label = f'{target["table"]} {key_label(target["key"], target_cols)}'
                else:
                    label = group["target"]
                up.append(f'<li><code>{safe(group["table"])}.({safe(",".join(c["from_columns"]))})'
                          f'</code> → {label}</li>')
        down = []
        for group in groups.get("downstream", []):
            constraint = group["constraint"]
            down.append(
                f'<li><code>{safe(group["table"])}.({safe(",".join(constraint["from_columns"]))})</code> '
                f'{group["total"]} 行'
                + (f'，<span class="rust">省略 {group["omitted"]}</span>' if group["truncated"] else "")
                + "</li>")
        cards.append(
            f'<article class="expand-node"><h3><a href="{safe(node["url"])}">'
            f'{safe(node["table"])} {key_label(node["key"], key_columns)}</a></h3>'
            f'<div class="expand-cols"><div><h4>上游外键</h4><ul>{"".join(up) or "<li class=muted>无</li>"}</ul></div>'
            f'<div><h4>入向引用</h4><ul>{"".join(down) or "<li class=muted>无</li>"}</ul></div></div></article>')
    if not cards:
        return ""
    frontier = len(result["truncated"]["depth_frontier"])
    boundary = (f'<p class="notice">深度 {result["depth"]}：更外一层的 {frontier} 个节点只收录未展开，'
                "点击节点标题可把它设为新起点继续追。</p>") if frontier else ""
    return ('<section class="paper expansion"><div class="panel-title"><h2>有界展开的相邻层</h2>'
            f'<span class="tag">深度 {result["depth"]} · 节点上限 {result["node_limit"]}</span></div>'
            f'{boundary}<div class="expand-grid">{"".join(cards)}</div></section>')


def compare_strip(result):
    current = result["image"]
    others = result.get("compare", [])
    items = [f'<li><span class="tag auto">当前</span> <b>{safe(current["label"])}</b> '
             f'<code>{safe(current["id"][:12])}</code></li>']
    for image in others:
        badge = '<span class="tag auto">有同键行</span>' if image["same_key"] else '<span class="tag">无此行</span>'
        items.append(
            f'<li>{badge} <a href="{safe(image["url"])}">{safe(image["label"])}</a> '
            f'<code>{safe(image["id"][:12])}</code> '
            f'<a class="compare-go" href="{safe(image["url"])}">查同键见证 →</a></li>')
    return ('<section class="paper compare"><div class="panel-title"><h2>跨镜像对照</h2>'
            '<span class="tag">见证严格按镜像隔离</span></div><ul>'
            + "".join(items) + "</ul></section>")


def entry_form(images, chosen=None, table_name="", key_text="", query=None):
    query = query or {}
    options = option("", "选择已登记镜像") + "".join(
        option(i["id"], f'{i["label"]} · {i["id"][:10]}', chosen=chosen and i["id"] == chosen["id"])
        for i in images)
    table_options = ""
    pk_hint = "选择镜像后列出该镜像的表与主键列。"
    assisted = ""
    if chosen:
        catalog_tables = chosen["catalog"]["tables"] if isinstance(chosen.get("catalog"), dict) \
            else chosen["catalog"]
        table_options = "".join(
            option(t["name"], f'{t["name"]}（主键 {"、".join(t["key"])}）',
                   chosen=t["name"] == table_name) for t in catalog_tables)
        definition = next((t for t in catalog_tables if t["name"] == table_name), None)
        if definition:
            pk_hint = "主键列顺序：" + "、".join(definition["key"])
            prefill = _prefill(definition, key_text, query)
            fields = []
            for index, (column, value, kind) in enumerate(prefill):
                type_options = "".join(option(k, label, chosen=k == kind) for k, label in (
                    ("auto", "自动"), ("i", "整数"), ("r", "实数"),
                    ("t", "文本"), ("b", "Base64 二进制")))
                fields.append(
                    f'<label><span class="input-title">{safe(column)}</span>'
                    f'<span class="pk-input"><select name="ty_{index}" aria-label="{safe(column)} 类型">'
                    f'{type_options}</select>'
                    f'<input name="v_{index}" value="{safe(value)}" placeholder="{safe(column)}"></span></label>')
            assisted = f'<div class="pk-fields">{"".join(fields)}</div>'
    depths = "".join(
        option(str(d), str(d),
               chosen=str(query.get("depth", str(DEFAULT_DEPTH))) == str(d))
        for d in range(1, 5))
    configure = '<button type="submit" name="step" value="columns" class="ghost">读取该表主键列</button>'
    submit = '<button class="primary" type="submit">查找上下游见证</button>' if assisted else ""
    return f"""
<section class="paper witness-entry">
  <div class="panel-title"><h2>指定镜像与主键</h2><span class="tag">引用见证入口</span></div>
  <form method="get" action="/witness" class="witness-form">
    <label><span class="input-title">镜像</span>
      <select name="image" data-witness-image>{options}</select></label>
    <label><span class="input-title">表</span>
      <select name="table" data-witness-table><option value="">选择表…</option>{table_options}</select></label>
    <p class="input-desc">{safe(pk_hint)}</p>
    {assisted}
    <details class="advanced"><summary>直接给 key（JSON 原子数组）/ 深度与限量</summary>
      <label><span class="input-title">key</span>
        <input name="key" value="{safe(key_text)}" placeholder='[["t","north"],["i","1"]]'></label>
      <label><span class="input-title">展开深度</span><select name="depth">{depths}</select></label>
      <label><span class="input-title">每组入向引用上限</span>
        <input name="limit" inputmode="numeric" value="{safe(query.get("limit", str(DEFAULT_FAN)))}"></label>
    </details>
    <div class="form-actions">{configure}{submit}</div>
  </form>
</section>"""


def _prefill(definition, key_text, query):
    if any(f"v_{i}" in query for i in range(len(definition["key"]))):
        return [(col, query.get(f"v_{i}", ""), query.get(f"ty_{i}", "auto"))
                for i, col in enumerate(definition["key"])]
    if key_text:
        try:
            import json
            atoms = json.loads(key_text)
            if isinstance(atoms, list) and len(atoms) == len(definition["key"]):
                result = []
                for col, part in zip(definition["key"], atoms):
                    if isinstance(part, list) and len(part) == 2:
                        kind, raw = part
                        value = raw if kind in {"i", "b"} else (
                            str(unpack(part)) if kind == "r" else raw if kind == "t" else "")
                        result.append((col, "" if value is None else str(value), kind))
                    else:
                        result.append((col, str(part), "auto"))
                return result
        except (ValueError, TypeError):
            pass
    return [(col, "", "auto") for col in definition["key"]]


def witness_result_page(result, images):
    image = result["image"]
    tables = {t["name"]: t for t in _catalog_of(result, images)}
    root_def = tables[result["table"]]
    root = result["root"]
    form = entry_form(images, chosen=_chosen_image(images, image["id"]),
                      table_name=result["table"], key_text=result["root"]["key_token"],
                      query={"depth": result["depth"], "limit": result["fan_limit"]})
    notices = "".join(f'<p class="notice">{safe(n)}</p>' for n in result["notices"])
    upstream = "".join(upstream_block(g, tables, image["id"]) for g in result["upstream"]) \
        or '<p class="muted">这一行没有声明外键，因此没有上游边。</p>'
    downstream = "".join(downstream_block(g, tables, image["id"]) for g in result["downstream"]) \
        or '<p class="muted">没有任何外键以该表为目标，因此没有入向边。</p>'
    api_query = urlencode({"table": result["table"], "key": root["key_token"],
                           "depth": result["depth"], "limit": result["fan_limit"]})
    content = f"""
<a class="back" href="/witness">← 引用见证入口</a>
<section class="heading witness-heading"><div>
  <h1>{safe(result["table"])} · 引用见证</h1>
  <p>{safe(image["label"])} · <code>{safe(image["id"])}</code></p>
  <p class="witness-keyline">{key_label(root["key"], root_def["key"])}</p>
</div><a class="api-link" href="/api/images/{image["id"]}/witness?{safe(api_query)}">JSON 接口 ↗</a></section>
{form}
<section class="paper root-card"><div class="panel-title"><h2>起点行</h2>
  <span class="tag">仅查询该镜像</span></div>
  {row_table(root_def, root["values"])}
</section>
{notices}
{cycle_block(result, tables)}
<div class="witness-grid">
  <section class="witness-col"><h2 class="col-title">上游：这一行的复合外键指向</h2>{upstream}</section>
  <section class="witness-col"><h2 class="col-title">下游：指向这一行的入向引用</h2>{downstream}</section>
</div>
{expansion_section(result, tables)}
{compare_strip(result)}
<p class="muted witness-rule">判定规则：复合外键的列按声明次序整体匹配；任一列 NULL 即无引用，不生成边。
每条边都标注所属外键约束与列来源；入向引用按约束分组，超出限量的行明确报告为未展开。</p>
"""
    return page("witness", "引用见证", content=content)


def _catalog_of(result, images):
    chosen = _chosen_image(images, result["image"]["id"])
    catalog = chosen["catalog"]
    return catalog["tables"] if isinstance(catalog, dict) else catalog


def _chosen_image(images, image_id):
    return next(i for i in images if i["id"] == image_id)


def witness_entry_page(images, query):
    chosen_id = query.get("image", "")
    chosen = next((i for i in images if i["id"] == chosen_id), None)
    table_name = query.get("table", "") if chosen else ""
    if chosen and isinstance(chosen.get("catalog"), str):
        import json
        chosen = dict(chosen, catalog=json.loads(chosen["catalog"]))
    if chosen and table_name and not any(t["name"] == table_name for t in chosen["catalog"]["tables"]):
        table_name = ""
    form = entry_form(images, chosen=chosen, table_name=table_name, key_text=query.get("key", ""),
                      query=query)
    content = f"""
<section class="heading"><div><h1>行引用见证</h1>
<p>指定一份镜像和一个主键，显示上下游相关行：沿复合外键查看上游父行与下游入向引用。查询严格限定在所选镜像内。</p></div></section>
{form}
<section class="paper witness-help"><div class="panel-title"><h2>这个入口能回答什么</h2></div>
<ul class="help-list">
<li><b>上游</b>：起点行声明的每个外键指向哪一（些）行；复合键按列序整体匹配。</li>
<li><b>下游</b>：哪些表的哪些外键实际引用了这一主键行；一对多时报告总数与未展开数量。</li>
<li><b>NULL</b>：复合外键任一列是 NULL 时按无引用处理，并指出是哪一列。</li>
<li><b>循环</b>：外键成环时标记回路而不是无限展开；每个见证行都能重新设为起点。</li>
<li><b>隔离</b>：相同主键在不同镜像上只返回该镜像自己的见证，可用底部对照切换来源。</li>
</ul>
<p class="muted">也可以在任意 <a href="/">镜像行窗口</a> 点击“见证 ↗”直接进入。</p>
</section>"""
    return page("witness", "行引用见证", content=content)
