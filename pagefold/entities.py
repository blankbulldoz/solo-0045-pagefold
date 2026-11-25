"""Explicit stable entity keys and primary-key rename groups.

The ordinary merge identity is the declared primary key.  That is correct for
row-level merge, but a primary-key text change is naturally represented there
as a delete of the old key plus an insert of the new key.  When callers
explicitly name another unique column set (an "entity key"), this module groups
the base/left/right incarnations of the same entity so one decision can accept
or reject a rename.

The module is deliberately additive: plans created without ``entity_keys`` keep
the old row-by-row item and decision format.
"""

import json

from .codec import canonical, fingerprint, unpack
from .errors import Problem
from .merge import ROLES, decision_role
from .rows import key_atom

ENTITY_OPTION_KEYS = (
    "entity_keys", "entity_specs", "entities", "identity_keys", "identity", "stable_keys",
    "natural_keys", "business_keys", "unique_keys", "entity_key", "stable_key",
    "natural_key", "business_key", "unique_key",
)
COLUMN_OPTION_KEYS = ("columns", "key", "entity_key", "stable_key", "natural_key",
                      "business_key", "unique_key", "fields")
CONSTRAINT_OPTION_KEYS = ("constraint", "constraint_name", "index", "index_name",
                          "key_name", "unique_index")


def entity_id(table, columns, stable_key):
    return fingerprint(["pagefold-entity-v1", table, list(columns), stable_key])


def reference_token(row, positions):
    """Canonical FK tuple with primary-key numeric identity normalization."""
    return canonical([key_atom(unpack(row[position])) for position in positions])


def _as_columns(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in COLUMN_OPTION_KEYS:
            if key in value:
                return _as_columns(value[key])
    if isinstance(value, (list, tuple)) and all(isinstance(part, str) for part in value):
        return list(value)
    raise Problem("entity_key_shape", "实体键须给出非空的列名或列名数组。", 400)


def normalize_specs(options):
    """Accept the small matrix of request shapes used by callers and tests."""
    if options is None:
        return []
    if isinstance(options, tuple):
        options = list(options)
    if isinstance(options, str):
        text = options.strip()
        if text[:1] in {"[", "{"}:
            try:
                options = json.loads(text)
            except ValueError as exc:
                raise Problem("entity_key_shape", "实体键声明须为合法 JSON。", 400) from exc
        elif text:
            parsed = []
            for chunk in text.split(";"):
                table, separator, column_text = chunk.partition(":")
                if not separator:
                    table, separator, column_text = chunk.partition("=")
                columns = [part.strip() for part in column_text.split(",") if part.strip()]
                parsed.append({"table": table.strip(), "columns": columns})
            options = parsed
        else:
            options = None
    raw = None
    if isinstance(options, list):
        raw = options
    elif isinstance(options, dict):
        for key in ENTITY_OPTION_KEYS:
            if key in options and options[key] not in (None, "", [], {}):
                raw = options[key]
                break
        if raw is None and not any(key in options for key in ("base", "left", "right", "revision", "item")):
            # A bare {"widget": ["uid"]} mapping is also convenient.
            if options and all(isinstance(value, (str, list, tuple, dict)) for value in options.values()):
                raw = options
    if raw in (None, "", [], {}):
        return []
    if isinstance(options, dict) and options.get("table") and (
            isinstance(raw, str) or (isinstance(raw, list) and all(isinstance(part, str) for part in raw))):
        raw = {"table": options["table"], "columns": [raw] if isinstance(raw, str) else raw}
    if isinstance(raw, str):
        return normalize_specs(raw)

    if isinstance(raw, dict):
        if "table" in raw and any(key in raw for key in COLUMN_OPTION_KEYS):
            entries = [raw]
        else:
            entries = [{"table": table, "columns": columns} for table, columns in raw.items()]
    elif isinstance(raw, list) and len(raw) == 2 and all(isinstance(part, str) for part in raw):
        # Convenient ("table", "entity-column") pair.
        entries = [{"table": raw[0], "columns": [raw[1]]}]
    else:
        entries = raw
    specs = []
    seen = set()
    for entry in entries:
        constraint_name = None
        if isinstance(entry, str):
            table, columns = entry, None
        elif isinstance(entry, dict):
            table = entry.get("table") or entry.get("name")
            columns = _as_columns(entry) if any(key in entry for key in COLUMN_OPTION_KEYS) else None
            constraint_name = next((entry[key] for key in CONSTRAINT_OPTION_KEYS if entry.get(key)), None)
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            table, columns = entry
        else:
            raise Problem("entity_key_shape", "无法识别实体键声明。", 400)
        if columns is None and not constraint_name:
            raise Problem("entity_key_shape", f"实体键 {table} 缺少 columns 或 constraint。", 400)
        if not isinstance(table, str) or not table:
            raise Problem("entity_key_shape", "实体键声明缺少 table。", 400)
        if columns is not None:
            columns = _as_columns(columns)
            if not columns:
                raise Problem("entity_key_shape", f"表 {table} 的实体键至少需要一列。", 400)
        signature = (table, tuple(columns or []), constraint_name or "")
        if signature not in seen:
            seen.add(signature)
            spec = {"table": table, "columns": columns}
            if constraint_name:
                spec["constraint"] = constraint_name
            specs.append(spec)
    return specs


def prepare_specs(catalog, options):
    specs = normalize_specs(options)
    tables = {table["name"]: table for table in catalog["tables"]}
    for spec in specs:
        table = tables.get(spec["table"])
        if table is None:
            raise Problem("unknown_table", f"找不到实体键所在表 {spec['table']}。", 404)
        if not spec.get("columns") and spec.get("constraint"):
            index = next((candidate for candidate in table.get("unique_indexes", [])
                          if candidate["name"] == spec["constraint"]), None)
            if index is None:
                raise Problem("unknown_entity_key",
                              f"表 {spec['table']} 找不到唯一键 {spec['constraint']}。", 400)
            spec["columns"] = index["columns"]
        missing = [column for column in spec["columns"] if column not in table["columns"]]
        if missing:
            raise Problem("unknown_column",
                          f"表 {spec['table']} 没有实体键列 {', '.join(missing)}。", 400)
        if len(set(spec["columns"])) != len(spec["columns"]):
            raise Problem("entity_key_shape", "实体键列不能重复。", 400)
        spec["key"] = table["key"]
    deduped = []
    seen_columns = set()
    for spec in specs:
        signature = (spec["table"], tuple(spec["columns"]))
        if signature not in seen_columns:
            seen_columns.add(signature)
            deduped.append(spec)
    return deduped


def _state(entry):
    if entry is None:
        return None
    return {"key_token": entry["key_token"], "key": json.loads(entry["key_token"]), "row": entry["row"]}


def build_entities(snapshots, catalog, specs):
    tables = {table["name"]: table for table in catalog["tables"]}
    groups = []
    for spec in specs:
        table_name = spec["table"]
        table = tables[table_name]
        indices = [table["columns"].index(column) for column in spec["columns"]]
        owners = {}
        key_owner = {role: {} for role in ROLES}
        for role in ROLES:
            role_map = {}
            for key_token, row in snapshots[role][table_name].items():
                stable = [row[index] for index in indices]
                if any(value[0] == "n" for value in stable):
                    raise Problem("entity_key_null",
                                  f"表 {table_name} 的明确实体键含有 NULL，不能用于识别同一实体。", 409)
                stable_token = canonical(stable)
                if stable_token in role_map:
                    raise Problem("entity_key_not_unique",
                                  f"表 {table_name} 的实体键 {stable} 在{role}支中不唯一。", 409)
                role_map[stable_token] = {"key_token": key_token, "row": row}
                key_owner[role][key_token] = stable_token
            owners[role] = role_map

        stable_tokens = set().union(*(owner.keys() for owner in owners.values()))
        for stable_token in sorted(stable_tokens):
            states = {role: _state(owners[role].get(stable_token)) for role in ROLES}
            signatures = {role: canonical(state) if state else None for role, state in states.items()}
            base_sig, left_sig, right_sig = (signatures[role] for role in ROLES)
            if left_sig == right_sig:
                automatic = None if left_sig == base_sig else "left"
            elif right_sig == base_sig:
                automatic = "left"
            elif left_sig == base_sig:
                automatic = "right"
            else:
                automatic = None
            if left_sig == base_sig == right_sig:
                continue

            base_state, left_state, right_state = (states[role] for role in ROLES)
            renamed = bool(base_state and any(
                states[role] and states[role]["key_token"] != base_state["key_token"]
                for role in ("left", "right")))
            if not base_state:
                kind = "insert"
            elif renamed:
                kind = "rename"
            else:
                present_branch_states = [state for role in ("left", "right")
                                          if (state := states[role]) is not None]
                if not present_branch_states:
                    kind = "delete"
                elif len(present_branch_states) < 2 and canonical(present_branch_states[0]) == base_sig:
                    # One branch deleted; the other did not modify the entity.
                    kind = "delete"
                elif len(present_branch_states) < 2:
                    kind = "modify_delete"
                else:
                    kind = "update"
            candidate_conflicts = []
            for role in ("left", "right"):
                state = states[role]
                if not state or (base_state and state["key_token"] == base_state["key_token"]):
                    continue
                target_token = state["key_token"]
                for observer in ROLES:
                    owner_token = key_owner[observer].get(target_token)
                    if owner_token and owner_token != stable_token:
                        candidate_conflicts.append({
                            "candidate_role": role, "observed_in": observer,
                            "key": state["key"], "other_entity": entity_id(
                                table_name, spec["columns"], json.loads(owner_token)),
                            "other_entity_key": json.loads(owner_token),
                        })
            eid = entity_id(table_name, spec["columns"], json.loads(stable_token))
            groups.append({
                "id": eid, "table": table_name, "entity_columns": spec["columns"],
                "entity_key": json.loads(stable_token), "automatic": automatic, "kind": kind,
                "rename": renamed, "states": states, "candidate_conflicts": candidate_conflicts,
                "key_conflict": bool(candidate_conflicts), "occupied": bool(candidate_conflicts),
                "item_ids": [],
            })
    return groups


def _specs_by_table(specs):
    result = {}
    for spec in specs:
        result.setdefault(spec["table"], []).append(spec)
    return result


def annotate_plan_items(items, groups, catalog, specs):
    table_specs = _specs_by_table(specs)
    tables = {table["name"]: table for table in catalog["tables"]}
    group_by_id = {group["id"]: group for group in groups}
    for item in items:
        spec_list = table_specs.get(item["table"], [])
        if not spec_list:
            continue
        table = tables[item["table"]]
        refs = {}
        for spec in spec_list:
            indices = [table["columns"].index(column) for column in spec["columns"]]
            for role in ROLES:
                row = item.get(role)
                if row is None:
                    continue
                stable_key = [row[index] for index in indices]
                eid = entity_id(item["table"], spec["columns"], json.loads(canonical(stable_key)))
                refs.setdefault(role, eid)
        if refs:
            item["entity_refs"] = refs
            unique = set(refs.values())
            item["entity_id"] = next(iter(unique)) if len(unique) == 1 else None
            item["entity_ids"] = sorted(unique)
            for eid in unique:
                group = group_by_id.get(eid)
                if group is not None and item["id"] not in group["item_ids"]:
                    group["item_ids"].append(item["id"])
    annotate_references(items, groups, catalog, specs)
    return items


def annotate_references(items, groups=None, catalog=None, specs=None):
    """Mark child-key differences that are only composite-FK follow-on changes."""
    if groups is None or catalog is None:
        return
    tables = {table["name"]: table for table in catalog["tables"]}
    # (child table, constraint id) -> referenced token -> role -> entity id.
    index = {}
    for child in catalog["tables"]:
        for constraint in child.get("foreign_key_constraints", []):
            parent_name = constraint["parent"]
            parent = tables.get(parent_name)
            if parent is None:
                continue
            role_map = {}
            for group in groups:
                if group["table"] != parent_name:
                    continue
                for role in ROLES:
                    state = group["states"][role]
                    if state is None:
                        continue
                    positions = [parent["columns"].index(column)
                                 for column in constraint["parent_columns"]]
                    token = reference_token(state["row"], positions)
                    role_map.setdefault(token, {})[role] = group["id"]
            index[(child["name"], constraint["id"])] = role_map

    fk_columns_by_table = {}
    for table in catalog["tables"]:
        columns = set()
        for constraint in table.get("foreign_key_constraints", []):
            columns.update(constraint["columns"])
        fk_columns_by_table[table["name"]] = columns

    for item in items:
        table = tables.get(item["table"])
        if table is None:
            continue
        descriptors = []
        all_reference_driven = True
        for constraint in table.get("foreign_key_constraints", []):
            positions = [table["columns"].index(column) for column in constraint["columns"]]
            role_entities = {}
            tokens = {}
            all_present = True
            for role in ROLES:
                row = item.get(role)
                if row is None:
                    all_present = False
                    continue
                token = reference_token(row, positions)
                tokens[role] = token
                eid = index.get((table["name"], constraint["id"]), {}).get(token, {}).get(role)
                if eid is not None:
                    role_entities[role] = eid
            static_reference = all_present and len(set(tokens.values())) == 1
            # A changing FK must be explained by an explicit entity group.
            explained = static_reference or (
                "base" in role_entities and all(
                    role in role_entities for role in tokens
                    if tokens[role] != tokens.get("base")))
            descriptors.append({
                "constraint": constraint["id"], "parent": constraint["parent"],
                "columns": constraint["columns"], "parent_columns": constraint["parent_columns"],
                "entities": role_entities, "matched": explained, "static": static_reference,
            })
            if not explained:
                all_reference_driven = False
        if descriptors:
            item["references"] = descriptors
            fk_columns = fk_columns_by_table[item["table"]]
            non_fk_positions = [index for index, column in enumerate(table["columns"])
                                if column not in fk_columns]
            rows = [item.get(role) for role in ROLES]
            present = [row for row in rows if row is not None]
            non_key_equal = bool(rows[0] is not None and rows[1] is not None and rows[2] is not None)
            if non_key_equal:
                signatures = {canonical([row[index] for index in non_fk_positions]) for row in rows}
                non_key_equal = len(signatures) == 1
            item["reference_driven"] = all_reference_driven and non_key_equal
        else:
            item["reference_driven"] = False


def decision_record(value):
    if value is None:
        return None
    if isinstance(value, str):
        role = decision_role(value)
        if role is None:
            return None
        status = "rejected" if role == "base" else "accepted"
        return {"choice": role, "status": status, "decision": status}
    if isinstance(value, dict) and decision_role(value):
        status = value.get("status") or value.get("decision")
        if status in ("accept", "accepted"):
            status = "accepted"
        elif status in ("reject", "rejected"):
            status = "rejected"
        elif value["choice"] == "base":
            status = "rejected"
        else:
            status = "accepted"
        return {"choice": value["choice"], "status": status, "decision": status}
    return None


def entity_view(group, decisions):
    record = decision_record(decisions.get(group["id"]))
    choice = record["choice"] if record else group["automatic"]
    if record:
        status = record["status"]
    else:
        status = "automatic" if group["automatic"] else "unresolved"
    view = dict(group)
    view.update({
        "choice": choice,
        "decision": status,
        "status": status,
        "accepted": status == "accepted",
        "rejected": status == "rejected",
        "explicit": record is not None,
        "keys": {role: group["states"][role]["key"] if group["states"][role] else None
                 for role in ROLES},
        "rows": {role: group["states"][role]["row"] if group["states"][role] else None
                 for role in ROLES},
    })
    return view


def unresolved_entities(groups, decisions):
    by_id = {group["id"]: group for group in groups}
    unresolved = []
    for group in groups:
        record = decision_record(decisions.get(group["id"]))
        if record is None and group["automatic"] is None:
            unresolved.append(group["id"])
    return unresolved


def target_role(group, decisions):
    record = decision_record(decisions.get(group["id"]))
    role = record["choice"] if record else group["automatic"]
    if role is None:
        raise Problem("unresolved", "存在未裁决的实体改名，请先接受或拒绝。", 409)
    return role


def normalize_entity_decision(data, group):
    raw = data.get("choice") or data.get("decision") or data.get("resolution")
    if not raw:
        if data.get("accepted") is True or data.get("accept") is True:
            raw = "accept"
        elif data.get("rejected") is True or data.get("reject") is True:
            raw = "reject"
    side = data.get("side") or data.get("role") or data.get("target")
    if isinstance(raw, str):
        compact = raw.replace("-", "_").lower()
        if compact in {"accept_left", "accepted_left", "choose_left", "left_accept"}:
            raw, side = "accept", "left"
        elif compact in {"accept_right", "accepted_right", "choose_right", "right_accept"}:
            raw, side = "accept", "right"
        elif compact in {"reject_rename", "rejected_rename", "keep_base", "reject_all"}:
            raw = "reject"
    if raw == "clear" or data.get("clear"):
        return None
    if raw in ("reject", "rejected"):
        return {"choice": "base", "status": "rejected", "decision": "rejected"}
    if raw in ("accept", "accepted"):
        if side in ("base",):
            return {"choice": "base", "status": "rejected", "decision": "rejected"}
        if side in ("left", "right"):
            role = side
        else:
            base_sig = canonical(group["states"]["base"]) if group["states"]["base"] else None
            changed = [role for role in ("left", "right")
                       if group["states"][role] and
                       canonical(group["states"][role]) != base_sig]
            if group["automatic"] in ("left", "right"):
                role = group["automatic"]
            elif len(changed) == 1:
                role = changed[0]
            elif group["states"]["left"] and group["states"]["left"] == group["states"]["right"]:
                role = "left"
            else:
                raise Problem("rename_side_required",
                              "左右支把同一实体改成了不同键，请明确接受左支或右支。", 400)
        return {"choice": role, "status": "accepted", "decision": "accepted"}
    if raw in ROLES:
        status = "rejected" if raw == "base" else "accepted"
        explicit = data.get("decision") or data.get("status")
        if explicit in ("reject", "rejected"):
            status = "rejected"
        if explicit in ("accept", "accepted"):
            status = "accepted"
        return {"choice": raw, "status": status, "decision": status}
    raise Problem("choice", "实体裁决须为 accept、reject、left、right、base 或 clear。", 400)
