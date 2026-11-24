"""从一行出发，沿复合外键与入向引用收集上下游见证。

所有查询都限定在某一份已登记镜像的只读挂载内：相同的表与主键放在不同镜像上
查询，只会得到该镜像自己的行与引用边。

边界约定：
- 复合外键中任一列是 NULL 时，按 SQLite 默认的 MATCH SIMPLE 语义视为“无引用”，
  不生成边，但会在结果里说明是哪一列导致。
- 图按节点去重后做有界展开，引用环只会被标记，不会导致无限递归。
- 一对多的入向引用按约束分组并限量取回，超出部分给出精确计数与未展开说明。
"""

import json
import sqlite3
from collections import OrderedDict
from urllib.parse import urlencode

from .codec import atom, canonical, unpack
from .errors import Problem
from .images import get_image
from .rows import key_atom
from .sqlite_image import ident, mounted

DEFAULT_DEPTH = 2
MAX_DEPTH = 4
DEFAULT_FAN = 50
MAX_FAN = 200
MAX_NODES = 240


def clamp_depth(value):
    if value is None:
        return DEFAULT_DEPTH
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise Problem("depth", "depth 须为 0 到 4 的整数。", 400) from exc
    return max(0, min(MAX_DEPTH, number))


def clamp_fan(value):
    if value is None:
        return DEFAULT_FAN
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise Problem("limit", "limit 须为正整数。", 400) from exc
    return max(1, min(MAX_FAN, number))


def catalog_tables(catalog):
    return {table["name"]: table for table in catalog["tables"]}


def find_table(tables, name):
    if name in tables:
        return tables[name]
    lowered = name.lower()
    for candidate, definition in tables.items():
        if candidate.lower() == lowered:
            return definition
    return None


def constraints_of(table, tables):
    """把 PRAGMA foreign_key_list 的逐列行合并成完整外键约束。"""
    groups = OrderedDict()
    for row in table.get("foreign_keys", []):
        groups.setdefault(row[0], []).append(row)
    result = []
    for fk_id, rows in groups.items():
        rows.sort(key=lambda item: item[1])
        referenced = find_table(tables, rows[0][2])
        key = referenced["key"] if referenced is not None else []
        to_columns = []
        for position, row in enumerate(rows):
            to_columns.append(row[4] if row[4] is not None
                              else key[row[1]] if row[1] < len(key)
                              else (key[position] if position < len(key) else None))
        result.append({
            "id": fk_id,
            "from_columns": [row[3] for row in rows],
            "to_table": rows[0][2],
            "to_columns": to_columns,
            "on_update": rows[0][5],
            "on_delete": rows[0][6],
            "match": rows[0][7],
        })
    return result


def _auto_literal(text):
    stripped = text.strip()
    if stripped == "":
        return None
    try:
        return int(stripped, 10)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return text


def _bindable(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        if not -(2 ** 63) <= value < 2 ** 63:
            raise Problem("bad_key", "主键整数超出 SQLite 的 64 位范围。", 400)
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise Problem("bad_key", "主键实数必须有限。", 400)
        return value
    if isinstance(value, (str, bytes)):
        return value
    raise Problem("bad_key", "主键含无法绑定的 JSON 值。", 400)


def _atoms_from_decoded(decoded):
    normalized = []
    for part in decoded:
        if isinstance(part, (list, tuple)) and len(part) == 2 and part[0] in {"n", "i", "r", "t", "b"}:
            normalized.append(key_atom(_bindable(unpack(list(part)))))
        else:
            value = _bindable(part)
            normalized.append(key_atom(value) if value is not None else ["n"])
    return normalized


def parse_key(table, raw, query=None):
    """返回 (原子键, 可绑定 Python 值)，兼容多种传参方式。"""
    columns = table["key"]
    atoms = None
    if isinstance(raw, (list, tuple)):
        decoded = list(raw)
        atoms = _atoms_from_decoded(decoded)
    elif raw is not None and raw != "":
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise Problem("bad_key",
                          "key 须是 JSON 数组，例如 [[\"t\",\"north\"],[\"i\",\"1\"]]。", 400) from exc
        if not isinstance(decoded, list):
            raise Problem("bad_key", "key 须是按主键顺序排列的数组。", 400)
        atoms = _atoms_from_decoded(decoded)
    elif query is not None and any(f"v_{index}" in query for index in range(len(columns))):
        atoms = []
        for index, column in enumerate(columns):
            text = query.get(f"v_{index}", "")
            kind = query.get(f"ty_{index}", "auto")
            try:
                if kind == "i":
                    value = int(text, 10)
                elif kind == "r":
                    value = float(text)
                elif kind == "b":
                    import base64
                    value = base64.b64decode(text, validate=True) if text else b""
                elif kind == "t":
                    value = text
                else:
                    value = _auto_literal(text)
            except (ValueError, TypeError) as exc:
                raise Problem("bad_key", f"主键列 {column} 的值无法按 {kind} 类型解读。", 400) from exc
            atoms.append(key_atom(value) if value is not None else ["n"])
    elif query is not None and all(column in query for column in columns):
        atoms = [key_atom(_auto_literal(query[column])) for column in columns]
    else:
        raise Problem("missing_key",
                      "请用 key 给出主键（JSON 原子数组），或按主键列名提供等值参数。", 400)
    if len(atoms) != len(columns):
        raise Problem("bad_key", f"主键需要 {len(columns)} 列（{ '、'.join(columns) }），"
                                 f"收到 {len(atoms)} 个值。", 400)
    if any(part[0] == "n" for part in atoms):
        raise Problem("null_key", "主键列不允许为 NULL，无法定位起点行。", 400)
    values = [_bindable(unpack(part)) for part in atoms]
    return atoms, values


def witness_url(image_id, table, key_atoms, view="witness", **extra):
    params = {"table": table, "key": canonical(key_atoms)}
    params.update({k: v for k, v in extra.items() if v is not None})
    return f"/images/{image_id}/{view}?" + urlencode(params)


def top_witness_url(image_id, table, key_atoms):
    return "/witness?" + urlencode({"image": image_id, "table": table, "key": canonical(key_atoms)})


def _select_row(db, definition, where_columns, values):
    columns = ", ".join(ident(name) for name in definition["columns"])
    where = " AND ".join(f"{ident(name)}=?" for name in where_columns)
    sql = f"SELECT {columns} FROM main.{ident(definition['name'])} WHERE {where}"
    try:
        found = db.execute(sql, values).fetchone()
    except (OverflowError, sqlite3.Error) as exc:
        raise Problem("bad_key", "主键值无法在镜像中比较。", 400) from exc
    return [atom(value) for value in found] if found is not None else None


def _count_and_rows(db, definition, where_columns, values, fan):
    where = " AND ".join(f"{ident(name)}=?" for name in where_columns)
    table_name = ident(definition["name"])
    total = db.execute(f"SELECT COUNT(*) FROM main.{table_name} WHERE {where}", values).fetchone()[0]
    columns = ", ".join(ident(name) for name in definition["columns"])
    rows = db.execute(
        f"SELECT {columns} FROM main.{table_name} WHERE {where} LIMIT ?",
        [*values, fan]).fetchall()
    return total, [[atom(value) for value in row] for row in rows]


def _count_null_like(db, definition, from_columns, values):
    """外键元组含 NULL、且所有非 NULL 列都与目标键相容的行数。

    这些行按 MATCH SIMPLE 不构成引用，却容易在只看单列时被误算成见证。
    """
    if not from_columns:
        return 0
    any_null = " OR ".join(f"{ident(col)} IS NULL" for col in from_columns)
    compatible = " AND ".join(
        f"({ident(col)} IS NULL OR {ident(col)}=?)" for col in from_columns)
    sql = (f"SELECT COUNT(*) FROM main.{ident(definition['name'])} "
           f"WHERE ({any_null}) AND {compatible}")
    try:
        return db.execute(sql, list(values)).fetchone()[0]
    except sqlite3.Error:
        return 0


class WitnessGraph:
    def __init__(self, db, image_id, image_label, tables, root_def, depth, fan):
        self.db = db
        self.image_id = image_id
        self.image_label = image_label
        self.tables = tables
        self.root_def = root_def
        self.depth = depth
        self.fan = fan
        self.nodes = OrderedDict()
        self.edges = []
        self._edge_keys = set()
        self.cycles = []
        self.notices = []
        self.truncated_groups = []
        self.unexpanded = 0
        self.root_id = None

    def node_id(self, table, key_atoms):
        return table + ":" + canonical(key_atoms)

    def make_node(self, definition, key_atoms, values=None, depth=0):
        nid = self.node_id(definition["name"], key_atoms)
        if nid in self.nodes:
            node = self.nodes[nid]
            if values is not None and node.get("values") is None:
                node["values"] = values
            return node
        if values is None:
            values = _select_row(self.db, definition, definition["key"],
                                 [unpack(part) for part in key_atoms])
        node = {
            "id": nid,
            "table": definition["name"],
            "key": key_atoms,
            "key_token": canonical(key_atoms),
            "values": values,
            "exists": values is not None,
            "depth": depth,
            "expanded": False,
            "url": witness_url(self.image_id, definition["name"], key_atoms, depth=self.depth),
        }
        self.nodes[nid] = node
        return node

    def add_edge(self, child, constraint, parent, kind):
        key = canonical([child["id"], constraint["id"], constraint["from_columns"], parent["id"]])
        if key in self._edge_keys:
            for edge in self.edges:
                if edge["key"] == key and kind not in edge["observed_as"]:
                    edge["observed_as"].append(kind)
            return next((edge for edge in self.edges if edge["key"] == key), None)
        self._edge_keys.add(key)
        edge = {
            "key": key,
            "kind": kind,
            "observed_as": [kind],
            "child": child["id"],
            "parent": parent["id"],
            "constraint": constraint,
            "source": f'{child["table"]}({", ".join(constraint["from_columns"])})',
            "target": f'{parent["table"]}({", ".join(constraint["to_columns"])})',
        }
        self.edges.append(edge)
        return edge

    def value_of(self, node, column):
        index = self.tables[node["table"]]["columns"].index(column)
        part = node["values"][index]
        return unpack(part)

    def upstream_group(self, node, definition, constraint):
        from_columns = constraint["from_columns"]
        if node["values"] is None:
            return None
        values = [self.value_of(node, column) for column in from_columns]
        null_columns = [column for column, value in zip(from_columns, values) if value is None]
        group = {
            "direction": "upstream",
            "holder_table": definition["name"],
            "table": definition["name"],
            "constraint": constraint,
            "fk": constraint,
            "source_node": node["id"],
            "null_columns": null_columns,
            "status": "matched",
            "matched": True,
            "witness": None,
            "target": None,
            "row": None,
            "rows": [],
            "witnesses": [],
            "explain": "",
        }
        if null_columns:
            group["status"] = "contains_null"
            group["matched"] = False
            group["explain"] = (
                f'复合外键（{"、".join(from_columns)}）中的 {"、".join(null_columns)} 为 NULL；'
                "按 MATCH SIMPLE 规则，任一列 NULL 即整组无引用，不会匹配任何上游行。")
            return group
        target_def = find_table(self.tables, constraint["to_table"])
        target_row = _select_row(self.db, target_def, constraint["to_columns"], values)
        if target_row is None:
            # 正常登记的镜像通过 foreign_key_check，悬垂只可能出现在手工构造的边界样本。
            synthetic = [key_atom(value) for value in values]
            target = {
                "id": f'missing:{target_def["name"]}:{canonical(synthetic)}',
                "table": target_def["name"], "key": synthetic, "values": None,
                "exists": False, "depth": node["depth"] + 1, "expanded": False,
                "url": "",
            }
            self.nodes[target["id"]] = target
            group["status"] = "dangling"
            group["matched"] = False
            group["target"] = target["id"]
            group["explain"] = "外键各列均非 NULL，但在该镜像中找不到匹配的上游行。"
        else:
            positions = [target_def["columns"].index(col) for col in target_def["key"]]
            target_atoms = [key_atom(unpack(target_row[pos])) for pos in positions]
            target = self.make_node(target_def, target_atoms, values=target_row,
                                    depth=node["depth"] + 1)
            group["witness"] = target["id"]
            group["target"] = target["id"]
            group["row"] = target["values"]
            group["rows"] = [target["values"]]
            group["witnesses"] = [target]
            self.add_edge(node, constraint, target, "upstream")
        return group

    def downstream_groups(self, node, definition):
        groups = []
        for child_def in self.tables.values():
            for constraint in constraints_of(child_def, self.tables):
                referenced_def = find_table(self.tables, constraint["to_table"])
                if referenced_def is None or referenced_def["name"] != definition["name"]:
                    continue
                # 只统计目标列能从当前行取值的约束（目标列即主键或唯一键列）。
                try:
                    values = [self.value_of(node, column) for column in constraint["to_columns"]]
                except ValueError:
                    continue
                if any(value is None for value in values):
                    continue
                total, raw_rows = _count_and_rows(
                    self.db, child_def, constraint["from_columns"], values, self.fan)
                null_like = _count_null_like(
                    self.db, child_def, constraint["from_columns"], values)
                witnesses = []
                for row_values in raw_rows:
                    key_atoms = [key_atom(unpack(row_values[child_def["columns"].index(column)]))
                                 for column in child_def["key"]]
                    child = self.make_node(child_def, key_atoms, values=row_values,
                                           depth=node["depth"] + 1)
                    witnesses.append(child["id"])
                    self.add_edge(child, constraint, node, "downstream")
                omitted = total - len(raw_rows)
                group = {
                    "direction": "downstream",
                    "holder_table": child_def["name"],
                    "table": child_def["name"],
                    "constraint": constraint,
                    "fk": constraint,
                    "source_node": node["id"],
                    "source_table": node["table"],
                    "source_key": node["key"],
                    "status": "truncated" if omitted > 0 else "matched",
                    "total": total,
                    "shown": len(raw_rows),
                    "count": len(raw_rows),
                    "returned": len(raw_rows),
                    "omitted": omitted,
                    "truncated": omitted > 0,
                    "witness_nodes": witnesses,
                    "witnesses": [self.nodes[nid] for nid in witnesses],
                    "rows": [self.nodes[nid]["values"] for nid in witnesses],
                    "null_columns": [],
                    "null_like": null_like,
                    "explain": "",
                }
                if null_like:
                    group["null_explain"] = (
                        f"另有 {null_like} 行的外键元组含 NULL（其余非空列即使与本键相容），"
                        "按 MATCH SIMPLE 任一列 NULL 即无引用，未计为见证。")
                else:
                    group["null_explain"] = ""
                if omitted > 0:
                    group["explain"] = (
                        f'入向引用 {child_def["name"]}（{"、".join(constraint["from_columns"])}）'
                        f"共 {total} 行，本页只展开前 {len(raw_rows)} 行，"
                        f"另有 {omitted} 行未展开；可用更大的 limit 或逐行重定起点查看。")
                    self.truncated_groups.append({
                        "table": child_def["name"], "total": total,
                        "shown": len(raw_rows), "omitted": omitted})
                groups.append(group)
        return groups

    def expand(self):
        root_id = self.node_id(self.root_def["name"], self.root_key)
        self.root_id = root_id
        self.groups_by_node = {}
        queue = [root_id]
        while queue:
            nid = queue.pop(0)
            node = self.nodes[nid]
            if node["expanded"]:
                continue
            if not node.get("exists", node.get("values") is not None):
                # 悬垂目标没有行体，无法继续沿它展开。
                node["expanded"] = True
                continue
            if len([n for n in self.nodes.values() if n["expanded"]]) >= MAX_NODES:
                self.unexpanded += 1 + len(queue)
                self.notices.append(
                    f"相关行超过展开上限 {MAX_NODES} 个节点，其余部分没有展开；"
                    "可逐行打开见证视图继续追踪。")
                break
            node["expanded"] = True
            definition = self.tables[node["table"]]
            upstream = [g for g in (self.upstream_group(node, definition, c)
                                    for c in constraints_of(definition, self.tables)) if g]
            downstream = self.downstream_groups(node, definition)
            self.groups_by_node[nid] = {"upstream": upstream, "downstream": downstream}
            if node["depth"] >= self.depth:
                # 深度边界：邻居已经收录，但不再继续展开它们。
                continue
            for group in upstream:
                tid = group.get("target")
                if tid and tid not in queue and not self.nodes[tid]["expanded"]:
                    queue.append(tid)
            for group in downstream:
                for tid in group["witness_nodes"]:
                    if tid not in queue and not self.nodes[tid]["expanded"]:
                        queue.append(tid)
        self.trace_upstream_cycles()
        self.detect_cycles()

    def trace_upstream_cycles(self, max_steps=96):
        """不受展示深度限制，沿“所持外键 -> 父行”做有界 DFS 找环。

        这样即使环长超过默认展开深度，也能给出循环见证；总步数有上限。
        """
        seen = {canonical(sorted(c["nodes"])) for c in self.cycles}
        budget = [max_steps]

        def walk(nid, chain):
            if budget[0] <= 0:
                return
            node = self.nodes.get(nid)
            if node is None or node.get("values") is None:
                return
            if nid in chain:
                ids = list(dict.fromkeys(chain[chain.index(nid):] + [nid]))
                signature = canonical(sorted(ids))
                if signature not in seen:
                    seen.add(signature)
                    self.cycles.append({
                        "nodes": ids, "edges": [], "traced": True,
                        "route": self._cycle_route(ids)})
                return
            budget[0] -= 1
            definition = self.tables[node["table"]]
            for constraint in constraints_of(definition, self.tables):
                from_columns = constraint["from_columns"]
                try:
                    values = [self.value_of(node, col) for col in from_columns]
                except ValueError:
                    continue
                if any(value is None for value in values):
                    continue
                target_def = find_table(self.tables, constraint["to_table"])
                if target_def is None:
                    continue
                row = _select_row(self.db, target_def, constraint["to_columns"], values)
                if row is None:
                    continue
                positions = [target_def["columns"].index(col) for col in target_def["key"]]
                target_atoms = [key_atom(unpack(row[pos])) for pos in positions]
                target = self.make_node(target_def, target_atoms, values=row,
                                        depth=node["depth"] + 1)
                target["traced"] = True
                self.add_edge(node, constraint, target, "upstream")
                walk(target["id"], chain + [nid])

        if self.root_id:
            walk(self.root_id, [])

    def detect_cycles(self):
        """只沿有向外键方向（子 -> 父）检测已经展开图中的环。

        每条 edge 无论从上游还是下游视角观察，child→parent 方向始终一致。
        """
        adjacency = {}
        for edge in self.edges:
            adjacency.setdefault(edge["child"], []).append((edge["parent"], edge))
        color = {}
        stack = []

        def visit(nid):
            color[nid] = 1
            for parent, edge in adjacency.get(nid, []):
                if color.get(parent, 0) == 1:
                    cycle_edges = list(stack) + [edge]
                    node_ids = _cycle_nodes(cycle_edges)
                    signature = canonical(sorted(node_ids))
                    if not any(canonical(sorted(c["nodes"])) == signature for c in self.cycles):
                        self.cycles.append({
                            "nodes": node_ids,
                            "edges": [item["key"] for item in cycle_edges],
                            "route": self._cycle_route(node_ids),
                        })
                elif color.get(parent, 0) == 0:
                    stack.append(edge)
                    visit(parent)
                    stack.pop()
            color[nid] = 2

        for nid in list(self.nodes):
            if color.get(nid, 0) == 0:
                visit(nid)
        if len(self.cycles) > 20:
            self.cycles = self.cycles[:20]
            self.notices.append("外键循环数量很多，仅列出前 20 组。")
        if self.cycles:
            self.notices.append(
                f"在已展开范围内检测到 {len(self.cycles)} 组外键循环；"
                "循环边只标记一次，不会重复展开。")

    def _cycle_route(self, node_ids):
        labels = []
        for nid in node_ids:
            node = self.nodes.get(nid)
            if node:
                key_text = "/".join(part[1] if part[0] != "n" else "NULL" for part in node["key"])
                labels.append(f'{node["table"]}[{key_text}]')
        return " → ".join(labels + labels[:1])

    def result(self):
        root = self.nodes[self.root_id]
        root_groups = self.groups_by_node.get(self.root_id, {"upstream": [], "downstream": []})
        depth_frontier = [n["id"] for n in self.nodes.values()
                          if not n["expanded"] and n["exists"] and not n.get("traced")]
        upstream = root_groups["upstream"]
        downstream = root_groups["downstream"]
        return {
            "image": {"id": self.image_id, "label": self.image_label},
            "image_id": self.image_id,
            "table": self.root_def["name"],
            "key": self.root_key,
            "depth": self.depth,
            "fan_limit": self.fan,
            "node_limit": MAX_NODES,
            "root": root,
            "row": root,
            # 主命名：上游（所持外键指向的父行）/ 下游（入向引用的子行）。
            "upstream": upstream,
            "downstream": downstream,
            # 常见同义命名，方便不同调用方。
            "parents": upstream,
            "outgoing": upstream,
            "children": downstream,
            "incoming": downstream,
            "references_to_root": downstream,
            "referenced_by": downstream,
            "groups_by_node": self.groups_by_node,
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
            "cycles": self.cycles,
            "notices": self.notices,
            "truncated": {
                "fan": self.truncated_groups,
                "unexpanded_nodes": self.unexpanded,
                "depth_frontier": depth_frontier,
            },
            "omitted": sum(group["omitted"] for group in self.truncated_groups),
        }


def _cycle_nodes(cycle_edges):
    nodes = []
    for edge in cycle_edges:
        for nid in (edge["child"], edge["parent"]):
            if nid not in nodes:
                nodes.append(nid)
    return nodes


def witness_graph(store_db, image_id=None, table_name=None, raw_key=None, query=None,
                  depth=None, fan=None):
    query = query or {}
    image_id = image_id or query.get("image") or query.get("image_id") or query.get("id")
    table_name = table_name or query.get("table") or query.get("relation")
    if raw_key is None:
        raw_key = query.get("key") or query.get("pk") or query.get("primary_key")
    if not image_id:
        raise Problem("missing_image", "请用 image 指定要查询的镜像摘要。", 400)
    image = get_image(store_db, image_id, body=True)
    catalog = json.loads(image["catalog"])
    tables = catalog_tables(catalog)
    definition = find_table(tables, table_name) if table_name else None
    if definition is None:
        raise Problem("unknown_table", "镜像中没有该表。", 404)
    key_atoms, key_values = parse_key(definition, raw_key, query)

    def first_present(*names):
        for name in names:
            value = query.get(name)
            if value is not None and value != "":
                return value
        return None

    depth_value = depth if depth is not None else first_present("depth", "levels", "level")
    fan_value = fan if fan is not None else first_present(
        "limit", "fan", "page_size", "per_page", "per")
    depth = clamp_depth(depth_value)
    fan = clamp_fan(fan_value)
    with mounted(image["body"]) as db:
        graph = WitnessGraph(db, image_id, image["label"], tables, definition, depth, fan)
        graph.root_key = key_atoms
        root = graph.make_node(definition, key_atoms, depth=0)
        if not root["exists"]:
            raise Problem("unknown_row",
                          f'镜像 {image_id[:12]} 中找不到表 {definition["name"]} 上的指定主键行。', 404)
        graph.expand()
        result = graph.result()
    result["compare"] = compare_images(store_db, image_id, definition, key_atoms)
    return result


def compare_images(store_db, current_id, definition, key_atoms):
    """其他已登记镜像中是否存在同表同键的起点，便于横向比较来源。"""
    images = store_db.execute("SELECT id,label,body,catalog FROM images ORDER BY rowid DESC").fetchall()
    items = []
    key_values = [unpack(part) for part in key_atoms]
    for row in images:
        if row["id"] == current_id:
            continue
        catalog = json.loads(row["catalog"])
        other = find_table(catalog_tables(catalog), definition["name"])
        exists = False
        if other is not None and other["key"] == definition["key"]:
            try:
                with mounted(row["body"]) as db:
                    exists = _select_row(db, other, other["key"], key_values) is not None
            except Exception:
                exists = False
        items.append({
            "id": row["id"],
            "label": row["label"],
            "same_key": exists,
            "url": top_witness_url(row["id"], definition["name"], key_atoms),
        })
    return items
