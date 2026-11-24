from .merge import ROLES
from .render import ROLE_LABELS

FINDING_LABELS = {
    "table_missing": "缺表或多余表",
    "column_missing": "列缺失或多余",
    "column_type": "列定义不同",
    "column_order": "列顺序不同",
    "key_order": "复合主键顺序不同",
    "index_missing": "索引缺失或多余",
    "index_definition": "索引定义不同",
}

FINDING_KIND = {
    "table": "表结构",
    "column": "列结构",
    "key": "主键顺序",
    "index": "显式索引",
}


def table_map(catalog):
    return {table["name"]: table for table in catalog["tables"]}


def index_map(table):
    return {index["name"]: index for index in table.get("indexes", [])}


def role_evidence(role, table):
    if table is None:
        return {"role": role, "present": False, "columns": [], "key": [], "indexes": []}
    return {
        "role": role, "present": True,
        "columns": [f'{column["name"]} {column["type"] or "无类型声明"}'
                    + (" NOT NULL" if column["notnull"] else "") for column in table["columns"]],
        "key": list(table["key"]),
        "indexes": [f'{index["name"]}（{"唯一" if index["unique"] else "普通"}，'
                    f'{"、".join(index["columns"])}）' for index in table.get("indexes", [])],
    }


def make_finding(kind, table_name, tables, code, detail, subject=None, affected=None):
    fallback = [role for role in ROLES if tables[role] is not None]
    finding = {"kind": kind, "type": code, "label": FINDING_LABELS[code], "table": table_name,
               "detail": detail, "roles": affected or fallback,
               "evidence": {role: role_evidence(role, tables[role]) for role in ROLES}}
    if subject is not None:
        finding["subject"] = subject
    return finding


def table_presence(name, tables, findings):
    missing = [role for role in ROLES if tables[role] is None]
    if not missing:
        return False
    labels = "、".join(ROLE_LABELS[role] for role in missing)
    present = [role for role in ROLES if tables[role] is not None]
    findings.append(make_finding(
        "table", name, tables, "table_missing",
        f'表 {name} 在 {labels} 中不存在，仅 {"、".join(ROLE_LABELS[role] for role in present)} 包含该表。',
        affected=present or ROLES))
    return True


def column_findings(name, tables, findings):
    sequences = {role: [column["name"] for column in tables[role]["columns"]]
                 for role in ROLES if tables[role] is not None}
    union = []
    for columns in sequences.values():
        for column in columns:
            if column not in union:
                union.append(column)
    for column in union:
        owners = {role: next((c for c in tables[role]["columns"] if c["name"] == column), None)
                  for role in ROLES if tables[role] is not None}
        lacking = [role for role, owner in owners.items() if owner is None]
        if lacking:
            findings.append(make_finding(
                "column", name, tables, "column_missing",
                f'列 {column} 在 {"、".join(ROLE_LABELS[role] for role in lacking)} 中缺失。', column,
                affected=[role for role in owners if role not in lacking]))
            continue
        shapes = {role: (owners[role]["type"], owners[role]["notnull"], owners[role]["default"])
                  for role in owners}
        if len(set(shapes.values())) != 1:
            baseline = next(iter(shapes))
            differing = [role for role in shapes if shapes[role] != shapes[baseline]]
            findings.append(make_finding(
                "column", name, tables, "column_type",
                f'列 {column} 的类型、非空或默认值声明在各库不一致。', column,
                affected=differing or list(owners)))
    shared = [column for column in union
              if all(column in sequences[role] for role in sequences)]
    orders = {role: [column for column in sequences[role] if column in shared] for role in sequences}
    if len(set(map(tuple, orders.values()))) != 1:
        baseline = next(iter(orders))
        affected = [role for role in orders if orders[role] != orders[baseline]]
        findings.append(make_finding(
            "column", name, tables, "column_order",
            f'表 {name} 的共同列在各库中的排列顺序不同。', affected=affected or list(orders)))


def key_finding(name, tables, findings):
    keys = {role: tuple(tables[role]["key"]) for role in ROLES if tables[role] is not None}
    if len(set(keys.values())) == 1:
        return
    baseline = next(iter(keys))
    affected = [role for role in keys if keys[role] != keys[baseline]]
    if len({tuple(sorted(key)) for key in keys.values()}) == 1:
        detail = f'表 {name} 的复合主键列相同，但各库主键列顺序不同。'
    else:
        detail = f'表 {name} 的主键列构成或顺序在各库不一致。'
    findings.append(make_finding("key", name, tables, "key_order", detail,
                                 affected=affected or list(keys)))


def index_findings(name, tables, findings):
    maps = {role: index_map(tables[role]) for role in ROLES if tables[role] is not None}
    names = []
    for mapping in maps.values():
        for index_name in mapping:
            if index_name not in names:
                names.append(index_name)
    for index_name in names:
        owners = {role: mapping.get(index_name) for role, mapping in maps.items()}
        lacking = [role for role, owner in owners.items() if owner is None]
        if lacking:
            findings.append(make_finding(
                "index", name, tables, "index_missing",
                f'显式索引 {index_name} 在 {"、".join(ROLE_LABELS[role] for role in lacking)} 中不存在。',
                index_name, affected=[role for role in owners if role not in lacking]))
            continue
        shapes = {role: (owners[role]["unique"], tuple(owners[role]["columns"]), owners[role]["sql"])
                  for role in owners}
        baseline = next(iter(shapes))
        differing = [role for role in shapes if shapes[role] != shapes[baseline]]
        if differing:
            findings.append(make_finding(
                "index", name, tables, "index_definition",
                f'显式索引 {index_name} 的唯一性、列顺序或定义语句在各库不一致。', index_name,
                affected=differing or list(owners)))


def diff_catalogs(catalogs, labels=None):
    labels = labels or {role: ROLE_LABELS[role] for role in ROLES}
    mapped = [table_map(catalog) for catalog in catalogs]
    tables = {role: mapped[pos] for pos, role in enumerate(ROLES)}
    names = []
    for mapping in tables.values():
        for name in mapping:
            if name not in names:
                names.append(name)
    findings = []
    for name in names:
        current = {role: tables[role].get(name) for role in ROLES}
        if table_presence(name, current, findings):
            continue
        column_findings(name, current, findings)
        key_finding(name, current, findings)
        index_findings(name, current, findings)
    findings.sort(key=lambda item: (item["table"], item["kind"], item.get("subject", "")))
    return {
        "code": "schema_mismatch",
        "roles": {role: {"label": labels[role], "signature": catalogs[pos]["signature"]}
                  for pos, role in enumerate(ROLES)},
        "summary": {kind: sum(1 for item in findings if item["kind"] == kind)
                    for kind in FINDING_KIND},
        "count": len(findings),
        "findings": findings,
    }
