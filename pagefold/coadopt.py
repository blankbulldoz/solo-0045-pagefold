"""联动采纳分析：解释计划当前候选集合能否一起采纳。

仅依据外键关系推断主键行之间的联动；唯一键、检查约束与数据库完整性
以重演结果为准。行身份与 rows.key_atom 一致：整数值的实数与整数同键。
可空复合外键只要留有一个 NULL 就不产生联动；三份镜像一致的行不参与解释。
"""

import json
from collections import defaultdict
from itertools import product

from .codec import canonical
from .images import get_image
from .merge import ROLES
from .plans import get_plan, plan_content

MAX_ENUMERATED = 6561
MAX_FIXES = 3


def normalized(atom):
    if atom[0] == "r":
        value = float.fromhex(atom[1])
        if value.is_integer() and -(2**63) <= value < 2**63:
            return ["i", str(int(value))]
    return atom


def foreign_keys(catalog):
    shapes = {table["name"]: table for table in catalog["tables"]}
    result = defaultdict(list)
    for table in catalog["tables"]:
        grouped = defaultdict(list)
        for row in table["foreign_keys"]:
            grouped[row[0]].append(row)
        for fk_id, parts in grouped.items():
            parts.sort(key=lambda part: part[1])
            parent = shapes[parts[0][2]]
            ref_columns = [part[4] if part[4] is not None else parent["key"][part[1]] for part in parts]
            result[table["name"]].append({
                "id": fk_id, "columns": [part[3] for part in parts],
                "positions": [table["columns"].index(part[3]) for part in parts],
                "ref_table": parts[0][2], "ref_columns": ref_columns,
                "to_key": sorted(ref_columns) == sorted(parent["key"])})
    return result, shapes


def collect_edges(items, fks, shapes):
    by_id = {item["id"]: item for item in items}
    by_key = {(item["table"], canonical(item["key"])): item for item in items}
    by_table = defaultdict(list)
    for item in items:
        by_table[item["table"]].append(item)
    loose = {}

    def loose_index(ref_table, ref_columns):
        token = (ref_table, tuple(ref_columns))
        if token not in loose:
            positions = [shapes[ref_table]["columns"].index(c) for c in ref_columns]
            index = defaultdict(set)
            for candidate in by_table.get(ref_table, ()):
                for role in ROLES:
                    row = candidate[role]
                    if row is not None:
                        key = canonical([normalized(row[p]) for p in positions])
                        index[key].add(candidate["id"])
            loose[token] = index
        return loose[token]

    edges = {}
    for item in items:
        for fk in fks.get(item["table"], ()):
            for role in ROLES:
                row = item[role]
                if row is None:
                    continue
                values = [normalized(row[pos]) for pos in fk["positions"]]
                if any(value[0] == "n" for value in values):
                    continue
                if fk["to_key"]:
                    order = [fk["ref_columns"].index(c) for c in shapes[fk["ref_table"]]["key"]]
                    target = by_key.get((fk["ref_table"], canonical([values[i] for i in order])))
                    targets = [target] if target is not None else []
                else:
                    found = loose_index(fk["ref_table"], fk["ref_columns"]).get(canonical(values), ())
                    targets = [by_id[item_id] for item_id in found]
                for target in targets:
                    if target["id"] == item["id"]:
                        continue
                    token = (item["id"], target["id"], fk["id"], canonical(values))
                    edge = edges.setdefault(token, {
                        "from": item["id"], "to": target["id"], "table": item["table"],
                        "constraint": fk["id"], "columns": fk["columns"], "values": values,
                        "ref_table": fk["ref_table"], "ref_columns": fk["ref_columns"], "roles": set()})
                    edge["roles"].add(role)
    return edges


def evaluate(members, edges, decisions):
    members = sorted(members, key=lambda item: (item["table"], canonical(item["key"])))
    by_id = {item["id"]: item for item in members}
    current = {item["id"]: item["automatic"] or decisions.get(item["id"]) for item in members}
    pending = [item for item in members if current[item["id"]] is None]
    variables = [item["id"] for item in members if item["automatic"] is None]
    fixed = {item["id"]: item["automatic"] for item in members if item["automatic"] is not None}

    def violated(edge, assign):
        source = assign.get(edge["from"])
        target = assign.get(edge["to"])
        return (source in edge["roles"] and target is not None
                and by_id[edge["to"]][target] is None)

    listed = []
    for edge in sorted(edges, key=lambda e: (e["table"], e["constraint"], canonical(e["values"]), e["from"])):
        listed.append({**edge, "roles": sorted(edge["roles"]),
                       "active": current.get(edge["from"]) in edge["roles"],
                       "violated": violated(edge, current)})
    exhaustive = 3 ** len(variables) <= MAX_ENUMERATED
    feasible = []
    if exhaustive:
        for combo in product(ROLES, repeat=len(variables)):
            assign = {**fixed, **dict(zip(variables, combo))}
            if not any(violated(edge, assign) for edge in edges):
                feasible.append(assign)
    compatible = None
    if exhaustive:
        compatible = [a for a in feasible if all(a[i] == r for i, r in current.items() if r)]
    if compatible is None:
        verdict = "clash" if any(e["violated"] for e in listed) else "pending" if pending else "compatible"
    elif not compatible:
        verdict = "clash"
    elif pending:
        verdict = "pending"
    else:
        verdict = "compatible"

    def note(item):
        if item["automatic"] is not None:
            return "automatic"
        if current[item["id"]] is None:
            return "pending"
        if verdict == "clash" and exhaustive:
            keep = any(assign[item["id"]] == current[item["id"]] for assign in feasible)
            return "keep" if keep else "change"
        return "linked"

    fixes = []
    if verdict == "clash" and exhaustive:
        def distance(assign):
            return sum(assign[i] != role for i, role in current.items() if role)
        for assign in sorted(feasible, key=distance)[:MAX_FIXES]:
            fixes.append({"changes": distance(assign), "items": {i: assign[i] for i in variables}})
    options = {}
    if verdict == "pending" and exhaustive:
        for item in pending:
            options[item["id"]] = sorted({a[item["id"]] for a in compatible})
    return {"verdict": verdict, "repairable": (len(feasible) > 0) if exhaustive else None,
            "exhaustive": exhaustive, "violations": sum(1 for e in listed if e["violated"]),
            "items": [{"id": item["id"], "table": item["table"], "key": item["key"],
                       "automatic": item["automatic"], "choice": current[item["id"]],
                       "note": note(item)} for item in members],
            "edges": listed, "fixes": fixes, "options": options}


def analyze(catalog, items, decisions):
    fks, shapes = foreign_keys(catalog)
    edges = collect_edges(items, fks, shapes)
    root = {item["id"]: item["id"] for item in items}

    def find(item_id):
        while root[item_id] != item_id:
            root[item_id] = root[root[item_id]]
            item_id = root[item_id]
        return item_id

    for edge in edges.values():
        left, right = find(edge["from"]), find(edge["to"])
        if left != right:
            root[right] = left
    members_of = defaultdict(list)
    for item in items:
        members_of[find(item["id"])].append(item)
    edges_of = defaultdict(list)
    for edge in edges.values():
        edges_of[find(edge["from"])].append(edge)
    order = lambda item: (item["table"], canonical(item["key"]))
    grouped = sorted((members for members in members_of.values() if len(members) > 1),
                     key=lambda members: min(order(i) for i in members))
    groups = []
    for number, members in enumerate(grouped, 1):
        group = evaluate(members, edges_of[find(members[0]["id"])], decisions)
        groups.append({"id": f"g{number}", **group})
    independent = sorted((members[0] for members in members_of.values() if len(members) == 1),
                         key=order)
    choice = lambda item: item["automatic"] or decisions.get(item["id"])
    summary = {"items": len(items), "independent": len(independent),
               "linked": sum(len(g["items"]) for g in groups if g["verdict"] != "clash"),
               "rechoose": sum(len(g["items"]) for g in groups if g["verdict"] == "clash"),
               "pending": sum(1 for item in items if choice(item) is None)}
    return {"summary": summary, "groups": groups,
            "independent": [{"id": item["id"], "table": item["table"], "key": item["key"],
                             "automatic": item["automatic"], "choice": choice(item)}
                            for item in independent]}


def plan_analysis(db, plan_id):
    plan = get_plan(db, plan_id)
    items, decisions = plan_content(plan)
    catalog = json.loads(get_image(db, plan["base"])["catalog"])
    return {"plan": plan_id, "revision": plan["revision"], "state": plan["state"],
            **analyze(catalog, items, decisions)}
