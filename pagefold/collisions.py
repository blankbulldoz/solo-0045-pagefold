"""碰撞举证：针对受阻重演还原相互碰撞的主键行。

脱离计划页面的支持人员工具。给定状态库中一次受阻重演（计划 + 版本），
以同一祖本镜像与同一裁决快照重放，定位被唯一索引拒绝的写入行以及
与之碰撞的既有行，标明每行来自未变祖本还是合流后写入，并按部分
唯一索引的谓词覆盖与复合键 NULL 语义给出行级对照证据。
"""

import base64
import json
import re
import sqlite3

from .attempts import get_attempt
from .codec import atom, canonical, fingerprint, unpack
from .constraints import check_database
from .errors import Problem
from .images import get_image
from .merge import selected
from .plans import get_plan, plan_content
from .render import ROLE_LABELS
from .replay import amendments, delete_statement, insert_statement
from .rows import key_atom, read_table
from .sqlite_image import ident, mounted

CONTRAST_LIMIT = 8
COLLATIONS = {"BINARY", "NOCASE", "RTRIM"}
INDEX_KINDS = {"pk": "主键", "u": "唯一约束", "c": "唯一索引"}
SKIPPED_REASONS = {
    "incoming_null": "受阻行在该索引的键列含 NULL；唯一索引中 NULL 互不相同，不可能碰撞",
    "incoming_excluded": "受阻行不满足该部分索引的谓词，未纳入索引",
}
UNEVALUATED_REASONS = {
    "expression": "表达式索引，无法展开求值",
    "collation": "使用非内建排序规则，无法求值",
    "predicate": "无法从索引定义解析谓词",
}


def load_failure(store, plan_id, revision):
    with store.connect() as db:
        plan = get_plan(db, plan_id)
        attempt = get_attempt(db, plan_id, revision)
        if attempt is None:
            raise Problem("unknown_attempt", "找不到该版本的重演记录。", 404)
        result = json.loads(attempt["result"])
        if result.get("status") != "blocked":
            raise Problem("not_blocked", "该版本的重演并未受阻，无碰撞可举证。", 409)
        items, current = plan_content(plan)
        source = "attempt"
        decisions = result.get("decisions")
        if decisions is None:
            if plan["revision"] != revision:
                raise Problem("decisions_unavailable",
                              "该受阻记录缺少裁决快照，且计划版本已推进，无法忠实重演。", 409)
            decisions, source = current, "current"
        ancestor = get_image(db, plan["base"], body=True)
    return plan, result, items, decisions, source, ancestor


def trace_replay(db, catalog, items, decisions, operations):
    """按 replay.rebuild 的语句顺序重放，并跟踪每行的来源。"""
    provenance = {}
    for table in catalog["tables"]:
        provenance[table["name"]] = {key: {"kind": "ancestor"} for key in read_table(db, table)}
    for item in items:
        role, _ = selected(item, decisions)
        token = canonical(item["key"])
        if role == "base" and token in provenance[item["table"]]:
            provenance[item["table"]][token] = {"kind": "ancestor", "adoption": "decision",
                                                "item": item["id"]}
    applied = 0
    for table in catalog["tables"]:
        name = table["name"]
        for item, _ in operations.get(name, []):
            if item["base"] is not None:
                db.execute(delete_statement(table), tuple(map(unpack, item["key"])))
                provenance[name].pop(canonical(item["key"]), None)
                applied += 1
        for item, row in operations.get(name, []):
            if row is None:
                continue
            try:
                db.execute(insert_statement(table), tuple(map(unpack, row)))
            except sqlite3.IntegrityError as exc:
                return {"table": table, "item": item, "row": row, "error": exc,
                        "provenance": provenance, "applied": applied}
            role = item["automatic"] or decisions.get(item["id"])
            provenance[name][canonical(item["key"])] = {
                "kind": "branch", "role": role,
                "adoption": "automatic" if item["automatic"] else "decision", "item": item["id"]}
            applied += 1
    return {"table": None, "provenance": provenance, "applied": applied}


def split_predicate(sql):
    """从 CREATE INDEX 语句中取出顶层 WHERE 谓词文本。"""
    if not sql:
        return None
    depth, quote, close = 0, None, -1
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "[":
            quote = "]"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close = i
                break
        i += 1
    if close < 0:
        return None
    rest = sql[close + 1:].strip()
    match = re.match(r"(?is)WHERE\b", rest)
    return rest[match.end():].strip() if match else None


def unique_indexes(db, table):
    indexes = []
    for _, name, unique, origin, partial in db.execute(f"PRAGMA main.index_list({ident(table)})"):
        if not unique:
            continue
        columns, expression = [], False
        for _, _, column, _, collation, key in db.execute(f"PRAGMA main.index_xinfo({ident(name)})"):
            if not key:
                continue
            expression = expression or column is None
            columns.append({"name": column, "collation": (collation or "BINARY").upper()})
        row = db.execute("SELECT sql FROM main.sqlite_schema WHERE type='index' AND name=?",
                         (name,)).fetchone()
        sql = row[0] if row else None
        indexes.append({"name": name, "origin": origin, "partial": bool(partial),
                        "columns": columns, "expression": expression,
                        "predicate": split_predicate(sql) if partial else None, "sql": sql})
    return indexes


def predicate_holds(db, columns, values, predicate):
    projection = ",".join(f"? AS {ident(column)}" for column in columns)
    return db.execute(f"SELECT 1 FROM (SELECT {projection}) WHERE ({predicate})",
                      tuple(values)).fetchone() is not None


def equality_clause(index):
    return " AND ".join(f"{ident(column['name'])} COLLATE {ident(column['collation'])}=?"
                        for column in index["columns"])


def matching_rows(db, table, index, key_values, extra):
    columns = ",".join(map(ident, table["columns"]))
    sql = f"SELECT {columns} FROM main.{ident(table['name'])} WHERE {equality_clause(index)}"
    if extra:
        sql += f" AND ({extra})"
    return db.execute(sql, tuple(key_values)).fetchall()


def row_evidence(table, raw, provenance):
    positions = [table["columns"].index(key) for key in table["key"]]
    key = [key_atom(raw[position]) for position in positions]
    origin = provenance.get(canonical(key), {"kind": "unknown"})
    return {"key": key, "row": [atom(value) for value in raw], "origin": origin}


def public_index(index):
    return {"name": index["name"], "origin": index["origin"],
            "columns": [column["name"] for column in index["columns"]],
            "collations": [column["collation"] for column in index["columns"]],
            "partial": index["partial"], "predicate": index["predicate"], "sql": index["sql"]}


def contrast_rows(db, table, index, key_values, provenance):
    """找出与受阻行看起来值相同却未碰撞的行，并说明各自通过的原因。"""
    columns = ",".join(map(ident, table["columns"]))
    name = ident(table["name"])
    contrasts = []
    if index["partial"]:
        sql = (f"SELECT {columns} FROM main.{name} WHERE {equality_clause(index)}"
               f" AND (({index['predicate']}) IS NOT TRUE) LIMIT {CONTRAST_LIMIT}")
        for raw in db.execute(sql, tuple(key_values)):
            contrasts.append({**row_evidence(table, raw, provenance), "kind": "predicate_excluded"})
    any_null = " OR ".join(f"{ident(column['name'])} IS NULL" for column in index["columns"])
    near = " AND ".join(f"({ident(column['name'])} COLLATE {ident(column['collation'])}=? "
                        f"OR {ident(column['name'])} IS NULL)" for column in index["columns"])
    sql = f"SELECT {columns} FROM main.{name} WHERE ({any_null}) AND {near}"
    if index["partial"]:
        sql += f" AND (({index['predicate']}) IS TRUE)"
    sql += f" LIMIT {CONTRAST_LIMIT}"
    positions = [table["columns"].index(column["name"]) for column in index["columns"]]
    for raw in db.execute(sql, tuple(key_values)):
        entry = {**row_evidence(table, raw, provenance), "kind": "null_distinct"}
        entry["null_columns"] = [index["columns"][i]["name"] for i, pos in enumerate(positions)
                                 if raw[pos] is None]
        contrasts.append(entry)
    return contrasts


def probe_attempt(store, plan_id, revision):
    plan, result, items, decisions, source, ancestor = load_failure(store, plan_id, revision)
    catalog = json.loads(ancestor["catalog"])
    operations = amendments(items, decisions)
    evidence = {
        "format": "pagefold-collision-evidence-v1",
        "plan": plan_id,
        "revision": revision,
        "inputs": {"ancestor": plan["base"], "plan_digest": fingerprint(items),
                   "decision_digest": fingerprint(decisions), "decisions_source": source},
        "recorded": result["problem"],
        "collisions": [],
    }
    with mounted(ancestor["body"], writable=True) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        failure = trace_replay(db, catalog, items, decisions, operations)
        evidence["applied"] = failure["applied"]
        table = failure["table"]
        if table is None:
            evidence["failure"] = None
            try:
                check_database(db)
            except Problem as exc:
                evidence["post_check"] = exc.as_dict()
                evidence["consistent"] = exc.code == evidence["recorded"]["code"]
            else:
                evidence["consistent"] = False
                evidence["notes"] = ["重放未复现受阻点；失败可能发生在重读核对阶段。"]
        else:
            item, row = failure["item"], failure["row"]
            values = [unpack(value) for value in row]
            by_column = dict(zip(table["columns"], values))
            role = item["automatic"] or decisions.get(item["id"])
            evidence["table"] = table["name"]
            evidence["columns"] = table["columns"]
            evidence["failure"] = {"sqlite_error": str(failure["error"])}
            evidence["incoming"] = {
                "item": item["id"], "key": item["key"], "row": row,
                "origin": {"kind": "branch", "role": role,
                           "adoption": "automatic" if item["automatic"] else "decision",
                           "item": item["id"]}}
            provenance = failure["provenance"][table["name"]]
            skipped, unevaluated, cleared = [], [], []
            for index in unique_indexes(db, table["name"]):
                if index["expression"]:
                    unevaluated.append({"index": index["name"], "reason": "expression"})
                    continue
                if any(column["collation"] not in COLLATIONS for column in index["columns"]):
                    unevaluated.append({"index": index["name"], "reason": "collation"})
                    continue
                key_values = [by_column[column["name"]] for column in index["columns"]]
                if any(value is None for value in key_values):
                    skipped.append({"index": index["name"], "reason": "incoming_null"})
                    continue
                if index["partial"]:
                    if index["predicate"] is None:
                        unevaluated.append({"index": index["name"], "reason": "predicate"})
                        continue
                    if not predicate_holds(db, table["columns"], values, index["predicate"]):
                        skipped.append({"index": index["name"], "reason": "incoming_excluded"})
                        continue
                clause = f"({index['predicate']}) IS TRUE" if index["partial"] else None
                found = matching_rows(db, table, index, key_values, clause)
                if not found:
                    cleared.append(index["name"])
                    continue
                evidence["collisions"].append({
                    "index": public_index(index),
                    "existing": [row_evidence(table, raw, provenance) for raw in found],
                    "contrasts": contrast_rows(db, table, index, key_values, provenance)})
            evidence["skipped"] = skipped
            evidence["unevaluated"] = unevaluated
            evidence["clear"] = cleared
            prefix = f"{table['name']} {canonical(item['key'])}："
            evidence["consistent"] = (evidence["recorded"]["code"] == "constraint"
                                      and evidence["recorded"]["detail"].startswith(prefix))
    evidence["digest"] = fingerprint({key: value for key, value in evidence.items() if key != "digest"})
    return evidence


def atom_text(value):
    if value[0] == "n":
        return "NULL"
    if value[0] == "t":
        return json.dumps(value[1], ensure_ascii=False)
    if value[0] == "b":
        return "x'" + base64.b64decode(value[1]).hex() + "'"
    if value[0] == "r":
        return repr(float.fromhex(value[1]))
    return value[1]


def key_text(key):
    return "(" + ", ".join(map(atom_text, key)) + ")"


def row_text(columns, row):
    return " · ".join(f"{column}={atom_text(value)}" for column, value in zip(columns, row))


def origin_text(origin):
    if origin["kind"] == "ancestor":
        if origin.get("adoption") == "decision":
            return "未变祖本（裁决保留祖本）"
        return "未变祖本（三支均未改动）"
    if origin["kind"] == "branch":
        label = ROLE_LABELS.get(origin["role"], origin["role"])
        adoption = "自动采纳" if origin["adoption"] == "automatic" else "裁决选定"
        return f"合流后写入 · {label}（{adoption}）"
    return "来源不明"


def index_title(index):
    title = f"{INDEX_KINDS.get(index['origin'], '唯一索引')} {index['name']}"
    return title + "（部分索引）" if index["partial"] else title


def index_definition(index):
    if index["sql"]:
        return index["sql"]
    definition = "UNIQUE (" + ", ".join(index["columns"]) + ")"
    return definition + (f" WHERE {index['predicate']}" if index["partial"] else "")


def verdict_text(index):
    columns = "、".join(index["columns"])
    if index["partial"]:
        return (f"两行均满足谓词（{index['predicate']}）而纳入索引，"
                f"索引列（{columns}）值完全相同 → 拒绝。")
    return f"两行索引列（{columns}）值完全相同 → 拒绝。"


def contrast_text(index, entry):
    if entry["kind"] == "predicate_excluded":
        return f"索引列值与受阻行相同，但不满足谓词（{index['predicate']}），未纳入索引 → 通过。"
    nulls = "、".join(entry["null_columns"])
    return f"已纳入索引，但索引列 {nulls} 为 NULL；唯一索引中 NULL 互不相同 → 通过。"


def render_report(evidence):
    lines = [f"碰撞举证 · 计划 {evidence['plan']} · 第 {evidence['revision']} 版重演"]
    recorded = evidence["recorded"]
    lines.append(f"受阻记录：{recorded['code']} — {recorded['detail']}")
    inputs = evidence["inputs"]
    lines.append(f"输入绑定：祖本 {inputs['ancestor'][:12]}… · 计划摘要 {inputs['plan_digest'][:12]}… · "
                 f"裁决摘要 {inputs['decision_digest'][:12]}…（裁决来源：{inputs['decisions_source']}）")
    if evidence.get("failure") is None:
        lines.append(f"重放复核：应用 {evidence['applied']} 项写入后未在写入阶段受阻。")
        if "post_check" in evidence:
            post = evidence["post_check"]
            lines.append(f"失败发生在关系校验阶段：{post['code']} — {post['detail']}")
        for note in evidence.get("notes", []):
            lines.append(f"说明：{note}")
        lines.append("本次受阻不涉及唯一索引碰撞，无相互碰撞的主键行。")
        lines.append(f"证据摘要：{evidence['digest']}")
        return "\n".join(lines)
    consistent = "与受阻记录一致" if evidence.get("consistent") else "与受阻记录不一致，请核对计划版本"
    lines.append(f"重放复核：应用 {evidence['applied']} 项写入后于同一失败点受阻（{consistent}）。")
    lines.append(f"SQLite 报告：{evidence['failure']['sqlite_error']}")
    incoming = evidence["incoming"]
    lines.append("")
    lines.append(f"[受阻写入] {evidence['table']} 主键 {key_text(incoming['key'])}")
    lines.append(f"  行值：{row_text(evidence['columns'], incoming['row'])}")
    lines.append(f"  来源：{origin_text(incoming['origin'])}")
    for collision in evidence["collisions"]:
        index = collision["index"]
        lines.append("")
        lines.append(f"[相互碰撞] {index_title(index)}")
        lines.append(f"  定义：{index_definition(index)}")
        for existing in collision["existing"]:
            lines.append(f"  既有行主键 {key_text(existing['key'])}")
            lines.append(f"  行值：{row_text(evidence['columns'], existing['row'])}")
            lines.append(f"  来源：{origin_text(existing['origin'])}")
        lines.append(f"  判定：{verdict_text(index)}")
        if collision.get("contrasts"):
            lines.append("  对照（看起来值相同却通过的行）：")
            for entry in collision["contrasts"]:
                lines.append(f"    · 主键 {key_text(entry['key'])}：{contrast_text(index, entry)}")
                lines.append(f"      来源：{origin_text(entry['origin'])}")
    if not evidence["collisions"]:
        lines.append("")
        lines.append("未找到与之碰撞的唯一索引行；失败可能来自 CHECK 或 NOT NULL 约束。")
        for entry in evidence.get("skipped", []):
            lines.append(f"  · 索引 {entry['index']}：{SKIPPED_REASONS[entry['reason']]}")
        for entry in evidence.get("unevaluated", []):
            lines.append(f"  · 索引 {entry['index']}：{UNEVALUATED_REASONS[entry['reason']]}")
    lines.append("")
    lines.append(f"证据摘要：{evidence['digest']}")
    return "\n".join(lines)
