import base64
import hashlib

from .codec import canonical
from .render import safe, values
from .sqlite_image import ident


ROLES = ("base", "left", "right")
ROLE_TITLES = {"base": "祖本", "left": "左支", "right": "右支"}
TYPE_TITLES = {"n": "NULL", "i": "整数", "r": "实数", "t": "文本", "b": "BLOB"}


def conflict_kind(item):
    base, left, right = item["base"], item["left"], item["right"]
    if base is not None and left is None and right is not None and right != base:
        return "delete-modify", "删改冲突 · 左删右改", "left"
    if base is not None and right is None and left is not None and left != base:
        return "delete-modify", "删改冲突 · 左改右删", "right"
    if base is not None and left is not None and right is not None and left != base and right != base and left != right:
        return "double-modify", "双改冲突", ""
    if base is None and left is not None and right is not None and left != right:
        return "add-add", "新增分歧", ""
    if item.get("automatic"):
        return "automatic", "自动采纳的单边/同值变化", ""
    return "difference", "行差异", ""


def table_meta(item, catalog):
    tables = {table["name"]: table for table in (catalog or {}).get("tables", [])}
    table = tables.get(item["table"])
    if table is not None:
        return table
    width = max((len(item[role] or []) for role in ROLES), default=0)
    key_count = len(item["key"])
    return {"name": item["table"], "columns": [f"column_{index + 1}" for index in range(width)],
            "types": ["" for _ in range(width)],
            "key": [f"column_{index + 1}" for index in range(key_count)]}


def blob_html(atom):
    raw = base64.b64decode(atom[1] or "", validate=True)
    size = len(raw)
    sha = hashlib.sha256(raw).hexdigest()
    hexed = raw.hex().upper()
    literal = f"X'{hexed}'"
    summary = "空 BLOB" if size == 0 else f"{size:,} B · SHA-256 {sha[:20]}…"
    return (
        f'<span class="atom atom-b" data-type="b" data-bytes="{size}" data-sha256="{sha}" '
        f'data-hex="{raw.hex()}" data-json="{safe(canonical(atom))}">'
        f'<span class="type-badge type-b">BLOB</span>'
        f'<code class="atom-code blob-summary">{safe(summary)}</code>'
        f'<details class="blob-check"><summary>无歧义核对：HEX / SQLite 字面量 / 完整 SHA-256</summary>'
        f'<div class="blob-check-grid">'
        f'<span>HEX / X\'…\'（{size} 字节）</span><code class="blob-hex">{safe(literal)}</code>'
        f'<span>SHA-256</span><code class="blob-sha">{sha}</code>'
        f'<span>JSON 编码</span><code class="blob-json-hint">["b","…"] 中的 Base64 见下方“JSON 语义”</code>'
        f'</div></details></span>'
    )


def atom_html(atom):
    kind = atom[0]
    encoded = safe(canonical(atom))
    if kind == "n":
        return (
            f'<span class="atom atom-null is-real-null" data-type="n" data-null-kind="sql-null" '
            f'data-json="{encoded}">'
            f'<span class="type-badge type-n">NULL</span>'
            f'<code class="atom-code null">NULL</code>'
            f'<small class="null-clarify">真 NULL（SQL 空值，不是文本）</small></span>'
        )
    if kind == "b":
        return blob_html(atom)

    badge = f'<span class="type-badge type-{kind}">{TYPE_TITLES[kind]}</span>'
    null_kind = ""
    if kind == "i":
        body = f'<code class="atom-code">{safe(atom[1])}</code>'
        note = '<small class="type-note">整数存储类</small>'
    elif kind == "r":
        number = float.fromhex(atom[1])
        body = (f'<code class="atom-code">{safe(format(number, ".17g"))}</code>'
                f'<small class="exact-hex">精确浮点编码 {safe(atom[1])}</small>')
        note = '<small class="type-note">实数存储类</small>'
    else:
        text = atom[1]
        if text == "NULL":
            clarify = '文本字符串 “NULL”（四个字符），不是真 NULL'
            extra = " is-text-null text-null"
            null_kind = ' data-null-kind="text-null"'
        else:
            clarify = f"文本字符串 · {len(text)} 个字符"
            extra = ""
            null_kind = ""
        body = f'<code class="atom-code text-value{extra}">&quot;{safe(text)}&quot;</code>'
        note = f'<small class="type-note">{safe(clarify)}</small>'
    return (f'<span class="atom atom-{kind}" data-type="{kind}"{null_kind} data-json="{encoded}">'
            f'{badge}{body}{note}</span>')


def presence(item, role):
    row, base = item[role], item["base"]
    if role == "base":
        if row is not None:
            return "present base-present", "存在 · 比较基准"
        return "absent base-absent", "缺席 · 祖本无此复合键"
    if row is None and base is not None:
        return "absent role-deleted", "行缺席 · 选择此侧将删除"
    if row is None:
        return "absent role-absent", "行缺席 · 不会新增"
    if base is None:
        return "present role-added", "存在 · 相对祖本新增"
    if row != base:
        return "present role-changed", "存在 · 已偏离祖本"
    return "present role-same", "存在 · 与祖本相同"


def presence_row(item):
    cells = []
    for role in ROLES:
        state, label = presence(item, role)
        chosen = " preview-role" if item.get("choice") == role else ""
        cells.append(f'<td class="presence-cell {state}{chosen}" data-role="{role}" '
                     f'data-present="{str(item[role] is not None).lower()}">'
                     f'<span class="presence-dot" aria-hidden="true"></span>{safe(label)}</td>')
    return f'<tr class="presence-row"><th scope="row">行状态</th>{"".join(cells)}</tr>'


def cell_html(item, role, index, column):
    row, base = item[role], item["base"]
    if row is None:
        state = "base-absent" if role == "base" else ("deleted" if base is not None else "absent")
        detail = "祖本此行缺席" if role == "base" else ("整行缺席" if state == "deleted" else "该侧无此行")
        chosen = " preview-role" if item.get("choice") == role else ""
        return (f'<td class="diff-cell cell-absent state-{state}{chosen}" data-role="{role}" '
                f'data-column="{safe(column)}" data-column-index="{index}" '
                f'data-present="false" data-state="{state}">'
                f'<span class="absent-mark">∅ 行缺席</span><small>{safe(detail)}</small></td>')

    atom = row[index]
    if role == "base":
        state, flag = "base", '<span class="diff-flag flag-base">基准</span>'
    elif base is None:
        state, flag = "added", '<span class="diff-flag flag-added">＋ 祖本无此行</span>'
    elif atom != base[index]:
        state, flag = "changed", '<span class="diff-flag flag-changed">≠ 异于祖本</span>'
    else:
        state, flag = "same", '<span class="diff-flag flag-same">＝ 同祖本</span>'
    chosen = " preview-role" if item.get("choice") == role else ""
    return (f'<td class="diff-cell state-{state}{chosen}" data-role="{role}" '
            f'data-column="{safe(column)}" data-column-index="{index}" data-present="true" '
            f'data-state="{state}" data-type="{atom[0]}">'
            f'{atom_html(atom)}{flag}</td>')


def column_rows(item, table):
    columns = table["columns"]
    types = table.get("types", [])
    key_columns = set(table.get("key", []))
    key_order = {name: index + 1 for index, name in enumerate(table.get("key", []))}
    rows = [presence_row(item)]
    for index, column in enumerate(columns):
        declared = types[index] if index < len(types) and types[index] else "无声明类型"
        key_badge = ""
        if column in key_columns:
            key_badge = f'<span class="pk-badge">主键 #{key_order[column]}</span>'
        cells = "".join(cell_html(item, role, index, column) for role in ROLES)
        rows.append(
            f'<tr class="column-row" data-column="{safe(column)}" data-column-index="{index}">'
            f'<th scope="row" class="column-head"><span class="column-name">{safe(column)}</span>'
            f'<code class="declared-type">{safe(declared)}</code>{key_badge}</th>{cells}</tr>'
        )
    return "".join(rows)


def sql_preview(table):
    name = table["name"]
    columns = table["columns"]
    key = table.get("key", columns[:1])
    delete = "DELETE FROM " + ident(name) + " WHERE " + " AND ".join(
        f"{ident(column)} COLLATE BINARY=?" for column in key)
    insert = (f"INSERT OR ABORT INTO {ident(name)} "
              f"({', '.join(map(ident, columns))}) VALUES ({', '.join('?' for _ in columns)})")
    return delete, insert


def action_for(item, role):
    row, base = item[role], item["base"]
    if row is None and base is not None:
        return "delete"
    if row is None:
        return "absent"
    if row != base:
        return "write"
    return "keep-base"


def outcome(item, role, table, can_pick):
    row, base = item[role], item["base"]
    action = action_for(item, role)
    delete_sql, insert_sql = sql_preview(table)
    if action == "delete":
        label = "将删除整行"
        description = "按复合主键清除；重演后重读时期望该键缺席。"
        sql = delete_sql
    elif action == "absent":
        label = "将保持缺席"
        description = "目标行不存在于这一侧；不会插入，也没有旧行可删。"
        sql = "-- 无目标行，不执行写入"
    elif action == "write":
        label = "将写入整行"
        description = "重演先按复合主键删除旧行，再插入下列 JSON 行；隐式 rowid 不作为身份。"
        sql = delete_sql + ";\n" + insert_sql + ";"
    else:
        label = "最终同祖本"
        description = "候选从祖本副本构建；该目标与祖本一致，因此无需额外变更。"
        sql = "-- 祖本副本已含此行，无需额外 SQL"

    chosen = item.get("choice") == role
    selected = " is-selected" if chosen else ""
    button = ""
    if can_pick:
        button = (f'<button type="button" class="pick-role" data-pick-role="{role}">'
                  f'在裁决框中选{ROLE_TITLES[role]}</button>')
    badge = '<span class="outcome-badge">当前已选</span>' if chosen else ""
    return (
        f'<section class="outcome outcome-{action}{selected}" data-role="{role}" '
        f'data-action="{action}" data-action-label="{safe(label)}" '
        f'data-row-json="{safe(canonical(row))}">'
        f'<header><h4>{ROLE_TITLES[role]}</h4><span class="action-pill">{label}</span>{badge}</header>'
        f'<p>{safe(description)}</p>'
        f'<pre class="sql-preview"><code>{safe(sql)}</code></pre>'
        f'<details class="json-review"><summary>JSON 语义</summary>'
        f'<code class="outcome-json">{safe(canonical(row))}</code></details>'
        f'{button}</section>'
    )


def render_entry(item, table, state, form):
    kind, kind_label, delete_role = conflict_kind(item)
    choice = item.get("choice")
    choice_label = "待裁决" if choice is None else f"已选{ROLE_TITLES[choice]}"
    badge_class = "conflict" if item.get("automatic") is None else "auto"
    can_pick = state == "open" and item.get("automatic") is None
    outcomes = "".join(outcome(item, role, table, can_pick) for role in ROLES)
    form_html = form or '<p class="closed-note">计划不在裁决中；以下对象和后果仅供核对。</p>'
    return (
        f'<article class="entry diff-entry" data-entry data-item-id="{item["id"]}" '
        f'data-table="{safe(item["table"])}" data-current-choice="{choice or ""}" '
        f'data-conflict-kind="{kind}" data-conflict-code="{kind.replace("-", "_")}" '
        f'data-delete-role="{delete_role}">'
        f'<div class="entry-head"><h3><span class="table-name">{safe(item["table"])}</span> '
        f'<span class="key-values">{values(item["key"])}</span></h3>'
        f'<span class="tag {badge_class}">{safe(kind_label)} · {safe(choice_label)}</span></div>'
        f'<div class="diff-scroll"><table class="diff-grid" data-table="{safe(item["table"])}" '
        f'data-key-json="{safe(canonical(item["key"]))}">'
        f'<thead><tr><th scope="col">列 / 声明类型</th>'
        + "".join(
            f'<th scope="col" class="role-head{(" preview-role" if choice == role else "")}" '
            f'data-role="{role}">{ROLE_TITLES[role]}</th>'
            for role in ROLES
        ) + '</tr></thead>'
        f'<tbody>{column_rows(item, table)}</tbody></table></div>'
        f'<section class="outcome-grid" aria-label="选择后果">{outcomes}</section>'
        f'<div class="decision-zone">{form_html}</div>'
        f'</article>'
    )
