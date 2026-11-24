import json

from .codec import canonical, fingerprint
from .render import ROLE_LABELS, STATE_LABELS, fragment, option, page, safe, values


def plan_links(plans):
    return "".join(f'<a class="plan-link" href="/plans/{p["id"]}">'
                   f'<span>{safe(p["left_label"])} <b>＋</b> {safe(p["right_label"])}</span>'
                   f'<small>{STATE_LABELS[p["state"]]} · 第 {p["revision"]} 版</small></a>' for p in plans)


def entries(plan):
    blocks = []
    for item in sorted(plan["items"], key=lambda item: bool(item["automatic"])):
        form = ""
        auto = item["automatic"]
        if plan["state"] == "open" and auto is None:
            options = option("clear", "保留未决", chosen=item["choice"] is None)
            options += "".join(option(role, label, chosen=role == item["choice"])
                               for role, label in ROLE_LABELS.items())
            form = fragment("decision", plan=plan["id"], revision=plan["revision"], item=item["id"], options=options)
        choice = ROLE_LABELS.get(item["choice"], "待裁决")
        chosen = ("自动 · " if auto else "") + choice
        blocks.append(fragment("entry", item=item["id"], table=safe(item["table"]), key=values(item["key"]),
                               choice=chosen, badge="auto" if auto else "conflict", form=form,
                               **{role: values(item[role]) for role in ROLE_LABELS}))
    return "".join(blocks) or '<div class="empty">三份镜像没有行差异。</div>'


def _foreign_ties(tables):
    """List (child, parent, {child column position: parent column position}) for declared FKs."""
    ties = []
    for name, table in tables.items():
        grouped = {}
        for row in table["foreign_keys"]:
            grouped.setdefault(row[0], []).append(row)
        for rows in grouped.values():
            parent = rows[0][2]
            if parent not in tables:
                continue
            # A reference without explicit parent columns points at the parent's
            # primary key, in declared key order.
            implied = [tables[parent]["columns"].index(column) for column in tables[parent]["key"]]
            columns = {}
            for position, row in enumerate(rows):
                target = row[4] if row[4] is not None else (implied[position] if position < len(implied) else None)
                if row[3] not in table["columns"] or target not in range(len(tables[parent]["columns"])):
                    columns = {}
                    break
                columns[table["columns"].index(row[3])] = target
            if columns and len(columns) == len(rows):
                ties.append((name, parent, columns))
    return ties


def _same_key(atom, other):
    """Compare key atoms with SQLite's numeric-equality rule for integers and reals."""
    if atom[0] in {"i", "r"} and other[0] in {"i", "r"}:
        return float(atom[1]) == float(other[1])
    return atom == other


def _key_token(key):
    return tuple(("num", float(atom[1])) if atom[0] in {"i", "r"} else ("atom", canonical(atom))
                 for atom in key)


def dependency_groups(items, catalog):
    """Group adjudicable rows sharing a declared foreign-key reference."""
    conflicts = [item for item in items if item["automatic"] is None]
    tables = {table["name"]: table for table in catalog["tables"]}
    parent = {item["id"]: item["id"] for item in conflicts}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[max(left, right)] = min(left, right)

    by_table = {}
    for item in conflicts:
        by_table.setdefault(item["table"], []).append(item)

    for name, ref, columns in _foreign_ties(tables):
        slots = []
        for key_column in tables[ref]["key"]:
            target = tables[ref]["columns"].index(key_column)
            source = next((child for child, parent_column in columns.items()
                           if parent_column == target), None)
            if source is None:
                slots = []
                break
            slots.append(source)
        if not slots:
            continue
        guardians = {}
        for guardian in by_table.get(ref, []):
            guardians.setdefault(_key_token(guardian["key"]), []).append(guardian)
        for child in by_table.get(name, []):
            for row in (child["left"], child["right"], child["base"]):
                if row is None:
                    continue
                for guardian in guardians.get(_key_token([row[source] for source in slots]), ()):
                    if all(_same_key(row[source], atom)
                           for source, atom in zip(slots, guardian["key"])):
                        union(child["id"], guardian["id"])

    groups = {}
    for item in conflicts:
        groups.setdefault(find(item["id"]), []).append(item)
    ordered = sorted(groups.values(), key=lambda group: (group[0]["table"], json.dumps(group[0]["key"])))
    return [{"name": " · ".join(sorted({item["table"] for item in group})), "items": group}
            for group in ordered]


def batch_panel(plan, groups):
    if plan["state"] != "open":
        return ""
    if not groups:
        return '<p class="muted">没有待裁决的冲突，成组裁决当前无可用条目。</p>'
    pickable = option("clear", "保留未决") + "".join(option(role, label) for role, label in ROLE_LABELS.items())
    blocks = []
    for number, group in enumerate(groups, 1):
        rows = []
        for item in group["items"]:
            choices = option("clear", "保留未决", chosen=item["choice"] is None) + "".join(
                option(role, label, chosen=role == item["choice"]) for role, label in ROLE_LABELS.items())
            current = ROLE_LABELS.get(item["choice"], "待裁决")
            rows.append(fragment("batch_row", item=item["id"], table=safe(item["table"]),
                                 key=values(item["key"]), current=safe(current), options=choices))
        blocks.append(fragment("batch_group", plan=plan["id"], revision=plan["revision"],
                               key=fingerprint(sorted(item["id"] for item in group["items"])),
                               number=number, name=safe(group["name"]), options=pickable,
                               rows="".join(rows)))
    return fragment("batch", groups=len(groups),
                    rows=sum(len(group["items"]) for group in groups), blocks="".join(blocks))


def attempts_list(attempts, revision):
    return "".join(fragment("attempt", revision=a["revision"],
                            status="通过" if a["status"] == "passed" else "受阻",
                            marker=" · 当前版" if revision == a["revision"] else " · 历史版",
                            evidence=safe(a.get("candidate_digest") or a["problem"]["detail"]),
                            changes=a["changed"]) for a in attempts) or '<p class="muted">暂无重演记录。</p>'


def action_form(plan, action, label, style="", disabled=False):
    return fragment("action", plan=plan["id"], revision=plan["revision"], action=action,
                    label=label, style=style, disabled=" disabled" if disabled else "")


def plan_page(plan, images, attempts, published):
    actions = []
    if plan["state"] == "open":
        passed = any(a["revision"] == plan["revision"] and a["status"] == "passed" for a in attempts)
        actions.append(action_form(plan, "rehearse", "重演当前版", "primary", bool(plan["unresolved"])))
        actions.append(action_form(plan, "seal", "封存成果", disabled=not passed))
    if plan["state"] != "archived":
        actions.append(action_form(plan, "archive", "归档计划", "quiet"))
    if published:
        actions.append(fragment("release", plan=plan["id"]))
    roles = "".join(f'<a href="/images/{i["id"]}"><small>{label}</small><b>{safe(i["label"])}</b>'
                    f'<code>{i["id"][:14]}</code></a>' for label, i in zip(ROLE_LABELS.values(), images))
    catalog = json.loads(images[0]["catalog"])
    groups = dependency_groups(plan["items"], catalog)
    return page("plan", "合流计划", plan=plan["id"], short=plan["id"][:12], roles=roles,
                state=STATE_LABELS[plan["state"]], revision=plan["revision"], unresolved=plan["unresolved"],
                count=len(plan["items"]), items=entries(plan), history=attempts_list(attempts, plan["revision"]),
                batch=batch_panel(plan, groups), actions="".join(actions))
