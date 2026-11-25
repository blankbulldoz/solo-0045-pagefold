import json
import sqlite3
from collections import defaultdict

from .codec import canonical, unpack
from .constraints import check_database
from .entities import target_role
from .errors import Problem
from .merge import selected
from .rows import key_atom, read_image
from .sqlite_image import ident, mounted, validate_header


def amendments(items, decisions):
    changed = defaultdict(list)
    for item in items:
        role, row = selected(item, decisions)
        if role is None:
            raise Problem("unresolved", "存在未裁决的行，请先完成裁决。", 409)
        if row != item["base"]:
            changed[item["table"]].append((item, row))
    return changed


def row_key_token(table, row):
    positions = [table["columns"].index(key) for key in table["key"]]
    return canonical([key_atom(unpack(row[position])) for position in positions])


def values_token(row, positions):
    return canonical([key_atom(unpack(row[position])) for position in positions])


def build_changes(items, decisions, catalog=None, specs=None, entities=None):
    """Build row operations and parent-key mappings.

    Without entity specs this is exactly the legacy row-by-row
    :func:`amendments` path.  With specs, spec tables are driven by entity
    groups; child rows whose only differences are foreign-key follow-ons are
    resolved by the referenced entity decision.
    """
    if not specs:
        return amendments(items, decisions), []
    if catalog is None or entities is None:
        raise Problem("entity_plan", "实体裁决缺少目录或实体分组。", 500)
    operations = defaultdict(list)
    group_by_id = {group["id"]: group for group in entities}
    spec_tables = {spec["table"] for spec in specs}
    tables = {table["name"]: table for table in catalog["tables"]}

    def reference_row(item):
        row = list(item["base"])
        table = tables[item["table"]]
        for ref in item.get("references", []):
            base_eid = ref.get("entities", {}).get("base")
            group = group_by_id.get(base_eid)
            if group is None:
                continue
            role = target_role(group, decisions)
            state = group["states"].get(role)
            if state is None:
                continue
            parent = tables[ref["parent"]]
            child_positions = [table["columns"].index(column) for column in ref["columns"]]
            parent_positions = [parent["columns"].index(column)
                                for column in ref["parent_columns"]]
            for child_position, parent_position in zip(child_positions, parent_positions):
                row[child_position] = state["row"][parent_position]
        return row

    for item in items:
        if item["table"] in spec_tables:
            continue
        if item.get("reference_driven"):
            row = reference_row(item)
            if row != item["base"]:
                handled_item = dict(item)
                handled_item["reference_resolved"] = True
                operations[item["table"]].append((handled_item, row))
            continue
        role, row = selected(item, decisions)
        if role is None:
            raise Problem("unresolved", "存在未裁决的行，请先完成裁决。", 409)
        if row != item["base"]:
            operations[item["table"]].append((item, row))

    mappings = []
    for group in entities:
        role = target_role(group, decisions)
        base_state = group["states"]["base"]
        target_state = group["states"].get(role)
        base_row = base_state["row"] if base_state else None
        target_row = target_state["row"] if target_state else None
        if target_row != base_row:
            old_key = base_state["key"] if base_state else target_state["key"]
            operation_item = {
                "id": f"entity:{group['id']}", "table": group["table"], "key": old_key,
                "base": base_row, "entity_id": group["id"],
                "target_key": target_state["key"] if target_state else None,
            }
            operations[group["table"]].append((operation_item, target_row))
        if base_state and target_state:
            mappings.append({
                "entity_id": group["id"], "table": group["table"],
                "base_row": base_state["row"], "target_row": target_state["row"],
            })
    return operations, mappings


def apply_rows(db, table, operations):
    name = ident(table["name"])
    predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    for item, _ in operations:
        if item["base"] is not None:
            db.execute(f"DELETE FROM main.{name} WHERE {predicate}", tuple(map(unpack, item["key"])))
    for item, row in operations:
        if row is not None:
            try:
                db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})", tuple(map(unpack, row)))
            except sqlite3.IntegrityError as exc:
                context_key = item.get("target_key") or item["key"]
                raise Problem("constraint", f"{table['name']} {canonical(context_key)}：{exc}") from exc


def insert_row(db, table, row, context_key):
    name = ident(table["name"])
    columns = ",".join(map(ident, table["columns"]))
    params = ",".join("?" for _ in table["columns"])
    try:
        db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})",
                   tuple(map(unpack, row)))
    except sqlite3.IntegrityError as exc:
        raise Problem("constraint", f"{table['name']} {context_key}：{exc}") from exc


def remap_references(db, catalog, expected, mappings, report=None, handled_keys=None):
    """Rewrite composite foreign keys after accepted entity-key renames."""
    handled_keys = handled_keys or {}
    tables = {table["name"]: table for table in catalog["tables"]}
    maps_by_constraint = {}
    for child in catalog["tables"]:
        for constraint in child.get("foreign_key_constraints", []):
            parent = tables.get(constraint["parent"])
            if parent is None:
                continue
            parent_positions = [parent["columns"].index(column)
                                 for column in constraint["parent_columns"]]
            value_map = {}
            for mapping in mappings:
                if mapping["table"] != constraint["parent"]:
                    continue
                old_token = values_token(mapping["base_row"], parent_positions)
                new_values = tuple(mapping["target_row"][pos] for pos in parent_positions)
                if old_token != canonical([key_atom(unpack(value)) for value in new_values]):
                    value_map[old_token] = new_values
            if value_map:
                maps_by_constraint[(child["name"], constraint["id"])] = (constraint, value_map)

    details = []
    for table in catalog["tables"]:
        row_map = expected[table["name"]]
        affected = {}
        for current_token, original in list(row_map.items()):
            if current_token in handled_keys.get(table["name"], set()):
                continue
            updated = list(original)
            changes = []
            for (child_name, constraint_id), (constraint, value_map) in maps_by_constraint.items():
                if child_name != table["name"]:
                    continue
                positions = [table["columns"].index(column) for column in constraint["columns"]]
                old_token = values_token(original, positions)
                new_values = value_map.get(old_token)
                if new_values is None:
                    continue
                for position, value in zip(positions, new_values):
                    if updated[position] != value:
                        changes.append({"column": table["columns"][position],
                                       "from": updated[position], "to": value})
                        updated[position] = value
            if changes:
                new_token = row_key_token(table, updated)
                affected[current_token] = (updated, new_token, changes)

        if not affected:
            continue
        predicate = " AND ".join(f"{ident(key)} COLLATE BINARY=?" for key in table["key"])
        # Delete every affected row first so mutually exchanged keys do not
        # collide during the rewrite.
        for current_token in affected:
            db.execute(f"DELETE FROM main.{ident(table['name'])} WHERE {predicate}",
                       tuple(map(unpack, json.loads(current_token))))
        for current_token, (updated, new_token, changes) in affected.items():
            insert_row(db, table, updated, new_token)
            details.append({"table": table["name"], "from_key": json.loads(current_token),
                            "to_key": json.loads(new_token), "changes": changes})
        for current_token in affected:
            row_map.pop(current_token, None)
        for _, (updated, new_token, _) in affected.items():
            row_map[new_token] = updated
    if report is not None:
        report["reference_updates"] = details
    return details


def rebuild(ancestor, catalog, operations, key_mappings=None, report=None):
    with mounted(ancestor, writable=True) as db:
        expected = read_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        handled_reference_keys = defaultdict(set)
        for table in catalog["tables"]:
            name = table["name"]
            changes = operations.get(name, [])
            for item, row in changes:
                # Rows with an explicit/automatic operation are already at
                # their adjudicated target.  Skip both old and target identity
                # so swap mappings cannot rewrite them a second time.
                handled_reference_keys[name].add(canonical(item["key"]))
                if row is not None:
                    handled_reference_keys[name].add(row_key_token(table, row))
            apply_rows(db, table, changes)
            # Remove all old identities first, then insert target identities.
            # This is necessary when two entities exchange primary keys.
            for item, _ in changes:
                expected[name].pop(canonical(item["key"]), None)
            for item, row in changes:
                if row is None:
                    continue
                target_token = canonical(item["target_key"]) if item.get("target_key") else row_key_token(table, row)
                expected[name][target_token] = row
        remap_references(db, catalog, expected, key_mappings or [], report,
                         handled_reference_keys)
        check_database(db)
        if read_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与计划目标不一致。")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data
