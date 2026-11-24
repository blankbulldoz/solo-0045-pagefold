"""重演阻断集合的收集与归类。

一次重演不再在遇到首个约束错误时立刻放弃，而是把同批写入中能够确认的
问题收集为三类证据：

- verified：本次重建中由 SQLite 实际验证过的原因（INSERT 错误、
  ``PRAGMA integrity_check`` / ``foreign_key_check`` 的结果）；
- unchecked：目标行没能写入、无法继续用数据库内检查复核的条目；
- gaps：尚不能下结论、但可能继续影响恢复路径的缺口（典型是失败行
  身上被首个错误掩盖的外键）。

本模块只做有界的工作：不增加额外试算轮次，外键身份定位仅针对
``foreign_key_check`` 已经报出的违规，按声明的外键做集合反查。
"""

import sqlite3

from .codec import canonical, unpack
from .rows import key_atom
from .sqlite_image import ident

FK_LIMIT = 12

KIND_LABELS = {
    "check": "CHECK 约束",
    "not_null": "NOT NULL",
    "unique": "唯一约束",
    "primary_key": "主键约束",
    "foreign_key": "外键约束",
    "integrity": "完整性检查",
    "row_conversion": "重读一致性",
    "sqlite": "SQLite 执行错误",
    "constraint": "约束",
}

ACTIONS = {
    "check": "在该行的祖本、左支或右支版本中改选满足 CHECK 的一版后重新重演。",
    "not_null": "改选给出非空值的来源版本，或先在来源镜像补齐必填列。",
    "unique": "调整裁决以消除重复键值：让冲突行缺席，或改选不碰撞的版本。",
    "primary_key": "消除同一主键的重复目标行后重新重演。",
    "foreign_key": "恢复被引用的父行（改选父表裁决），或改选/删除孤立的引用行。",
    "integrity": "祖本或候选页已损坏，需更换可信来源后再合流。",
    "row_conversion": "写入后的实际行与计划目标不一致，需要核对类型编码。",
    "sqlite": "重建过程被 SQLite 错误中断，未尝试的条目和后续检查均不可信。",
    "constraint": "按 SQLite 给出的约束信息修正对应行的裁决。",
}

# sqlite3.IntegrityError.sqlite_errorcode 的扩展结果码。
_EXTENDED_KINDS = {
    275: "check",
    787: "foreign_key",
    1299: "not_null",
    1555: "primary_key",
    2067: "unique",
}


def classify(exc):
    """返回 (约束类别, 原始信息)，优先使用扩展结果码。"""
    kind = _EXTENDED_KINDS.get(getattr(exc, "sqlite_errorcode", None))
    message = str(exc)
    if kind is None:
        upper = message.upper()
        if message.startswith("CHECK"):
            kind = "check"
        elif "PRIMARY KEY" in upper:
            kind = "primary_key"
        elif "UNIQUE" in upper:
            kind = "unique"
        elif "NOT NULL" in upper:
            kind = "not_null"
        elif "FOREIGN KEY" in upper:
            kind = "foreign_key"
        else:
            kind = "constraint"
    return kind, message


def constraint_ref(kind, message):
    """从 SQLite 信息里取出约束表达式或 “表.列” 引用。"""
    if ": " in message:
        tail = message.split(": ", 1)[1].strip()
        return tail or None
    if ":" in message:
        return message.split(":", 1)[1].strip() or None
    return None


def group_foreign_keys(table):
    """把 PRAGMA foreign_key_list 的原始行按约束 id 归组。"""
    groups = {}
    order = []
    for row in table.get("foreign_keys", []):
        fid, seq, parent, frm, to, on_update, on_delete, match = row
        if fid not in groups:
            group = {"id": fid, "parent": parent, "pairs": [],
                     "on_update": on_update, "on_delete": on_delete}
            groups[fid] = group
            order.append(fid)
        groups[fid]["pairs"].append((seq, frm, to))
    result = []
    for fid in order:
        group = groups[fid]
        group["pairs"] = [(frm, to) for _, frm, to in sorted(group["pairs"])]
        result.append(group)
    return result


def target_columns(group, parent_table):
    explicit = [to for _, to in group["pairs"]]
    if all(explicit):
        return explicit
    if parent_table is not None:
        return list(parent_table["key"])
    return explicit


class Report:
    """收集一次 rebuild 内的全部证据，最后生成稳定的 JSON 结构。"""

    def __init__(self, catalog):
        self.catalog = catalog
        self.tables = {table["name"]: table for table in catalog.get("tables", [])}
        self._uid_seed = 0
        self.verified = []
        self.failures = []
        self.other_gaps = []
        self.fk_legacy = None
        self.integrity_legacy = None

    def _uid(self):
        self._uid_seed += 1
        return self._uid_seed

    # -- 收集 ----------------------------------------------------------------

    def insert_failure(self, table, item, row, exc):
        kind, message = classify(exc)
        uid = self._uid()
        key = item.get("key")
        self.verified.append({
            "uid": uid, "state": "verified", "source": "insert", "kind": kind,
            "code": "constraint", "table": table["name"], "key": key,
            "item_id": item.get("id"),
            "constraint": constraint_ref(kind, message), "message": message,
            "detail": f"{table['name']} {canonical(key)}：{message}",
            "action": ACTIONS.get(kind, ACTIONS["constraint"]),
        })
        self.failures.append({"uid": uid, "table": table["name"], "key": key,
                              "item_id": item.get("id"), "row": row})

    def integrity_failure(self, message):
        uid = self._uid()
        self.integrity_legacy = message
        self.verified.append({
            "uid": uid, "state": "verified", "source": "integrity_check",
            "kind": "integrity", "code": "integrity", "table": None, "key": None,
            "constraint": None, "message": message,
            "detail": f"SQLite 完整性检查失败：{message}",
            "action": ACTIONS["integrity"],
        })
        self.other_gaps.append({
            "code": "integrity_scope", "table": None, "key": None, "linked_uid": uid,
            "detail": "integrity_check(1) 遇到第一个问题即停止，数据库里可能还有未展开的损坏。",
            "affects": "它不改变“受阻”结论，但意味着当前阻断集合未必列全所有物理损坏。",
        })

    def foreign_key_finding(self, table_name, key, parent, fkid):
        table = self.tables.get(table_name)
        uid = self._uid()
        ref = f"{table_name} → {parent}（约束 {fkid}）"
        self.verified.append({
            "uid": uid, "state": "verified", "source": "foreign_key_check",
            "kind": "foreign_key", "code": "foreign_key", "table": table_name, "key": key,
            "constraint": f"{table_name}.fk{fkid}->{parent}", "message": ref,
            "detail": f"外键检查失败：{ref}", "action": ACTIONS["foreign_key"],
        })

    def foreign_key_truncated(self, shown):
        self.other_gaps.append({
            "code": "foreign_key_truncated", "table": None, "key": None, "linked_uid": None,
            "detail": f"PRAGMA foreign_key_check 最多展开 {FK_LIMIT} 条，后面还有未列出的违规。",
            "affects": f"已列出的 {shown} 条足以维持“受阻”结论，但完整的待修复集合被截断。",
        })

    def check_unavailable(self, check, exc):
        self.other_gaps.append({
            "code": "check_failed", "table": None, "key": None, "linked_uid": None,
            "detail": f"{check} 没能完成：{exc}",
            "affects": "该项数据库级验证缺失，无法据此判断候选是否满足全部约束。",
        })

    def abort(self, exc, operations, attempted):
        if isinstance(exc, sqlite3.IntegrityError):
            kind, message = classify(exc)
        else:
            kind, message = "sqlite", str(exc)
        uid = self._uid()
        self.verified.append({
            "uid": uid, "state": "verified", "source": "apply", "kind": kind,
            "code": "sqlite_replay", "table": None, "key": None,
            "constraint": None, "message": message, "detail": message,
            "action": ACTIONS["sqlite"],
        })
        unchecked = []
        for table_name, ops in operations.items():
            for item, _row in ops:
                token = canonical(item["key"]) if item.get("key") is not None else None
                if (table_name, token) in attempted:
                    continue
                unchecked.append({
                    "uid": self._uid(), "state": "unchecked", "table": table_name,
                    "key": item.get("key"), "item_id": item.get("id"),
                    "blocked_uid": uid,
                    "reason": "apply_aborted",
                    "detail": "重建在处理到该条目前中断，该条目没有被完整应用和检查。",
                    "foreign_keys": [],
                })
        self._abort_unchecked = unchecked
        self.other_gaps.append({
            "code": "checks_skipped", "table": None, "key": None, "linked_uid": uid,
            "detail": "应用阶段异常中断后，完整性、外键及重读校验的结论都不完整。",
            "affects": "即使已列出的问题被修复，也需要一次完整的重新重演才能给出通过结论。",
        })

    # -- 被掩盖外键的静态/目标态分析 ----------------------------------------

    def _target_state(self, group, values_by_target, expected, failed_index):
        parent = group["parent"]
        parent_table = self.tables.get(parent)
        bucket = expected.get(parent, {})
        cols = target_columns(group, parent_table)
        if parent_table is not None and cols == list(parent_table["key"]):
            target_key = [key_atom(values_by_target[col]) for col in parent_table["key"]]
            token = canonical(target_key)
            if token not in bucket:
                return "absent", target_key
            if failed_index.get((parent, token)):
                return "parent_blocked", target_key
            return "present", target_key
        # 外键引用非主键唯一列时，在计划目标态里做有界匹配。
        if parent_table is None:
            return "absent", None
        for encoded_row in bucket.values():
            raw = tuple(unpack(atom) for atom in encoded_row)
            row_map = dict(zip(parent_table["columns"], raw))
            if all(row_map.get(col) == value for col, value in values_by_target.items()):
                target_key = [key_atom(row_map[k]) for k in parent_table["key"]]
                token = canonical(target_key)
                state = "parent_blocked" if failed_index.get((parent, token)) else "present"
                return state, target_key
        target_key = [key_atom(values_by_target[col]) for col in cols if col in values_by_target]
        return "absent", target_key

    def associated_foreign_keys(self, failure, expected, failed_index):
        table = self.tables.get(failure["table"])
        encoded_row = failure["row"]
        if table is None or encoded_row is None:
            return []
        positions = {column: index for index, column in enumerate(table["columns"])}
        raw_values = tuple(unpack(atom) for atom in encoded_row)
        descriptors = []
        for group in group_foreign_keys(table):
            columns = [frm for frm, _ in group["pairs"]]
            if any(column not in positions for column in columns):
                continue
            values = [raw_values[positions[column]] for column in columns]
            parent_table = self.tables.get(group["parent"])
            ref_columns = target_columns(group, parent_table)
            descriptor = {
                "constraint_id": group["id"],
                "columns": columns,
                "references": {"table": group["parent"], "columns": ref_columns},
            }
            if any(value is None for value in values):
                # 默认 MATCH SIMPLE：任一外键列为 NULL 时不强制引用。
                descriptor.update(target_state="nullable_skipped", key=None)
                descriptors.append(descriptor)
                continue
            values_by_target = {
                to if to else ref_columns[index]: value
                for index, ((_frm, to), value) in enumerate(zip(group["pairs"], values))
            }
            state, target_key = self._target_state(group, values_by_target, expected, failed_index)
            descriptor.update(target_state=state, key=target_key)
            descriptors.append(descriptor)
        return descriptors

    # -- 定稿 ---------------------------------------------------------------

    def finalize(self, expected):
        failed_index = {
            (failure["table"], canonical(failure["key"])): failure["uid"]
            for failure in self.failures if failure.get("key") is not None
        }
        unchecked = list(getattr(self, "_abort_unchecked", []))
        gaps = list(self.other_gaps)
        for failure in self.failures:
            try:
                fks = self.associated_foreign_keys(failure, expected, failed_index)
            except Exception as exc:  # 诊断逻辑本身不能盖过已验证的首要原因
                fks = []
                gaps.append({
                    "code": "analysis_failed", "table": failure["table"], "key": failure["key"],
                    "linked_uid": failure["uid"],
                    "detail": f"该条目的关联外键分析未能完成：{exc}",
                    "affects": "与该条目相关的外键是否成立仍然未知，需要修复首错后重新重演确认。",
                })
            unchecked.append({
                "uid": self._uid(), "state": "unchecked", "table": failure["table"],
                "key": failure["key"], "item_id": failure.get("item_id"),
                "blocked_uid": failure["uid"],
                "reason": "insert_failed",
                "detail": "该目标行没能写入数据库，SQLite 不会保存它；针对实际存储行的外键检查和该行其余约束无法继续验证。",
                "remaining": [
                    "同一条 INSERT 上 SQLite 未回报的其余约束",
                    "PRAGMA foreign_key_check 对该行的引用检查",
                ],
                "foreign_keys": fks,
            })
            for fk in fks:
                if fk.get("target_state") not in {"absent", "parent_blocked"}:
                    continue
                if fk["target_state"] == "absent":
                    detail = ("该行写入已被首个约束错误拦下，行不在库中，"
                              "PRAGMA foreign_key_check 因而看不到此外键；按计划目标态，被引用父行不存在。")
                    affects = "修复当前原因后，这个外键缺失大概率会接着阻断，需要同时恢复父行或改选引用值。"
                else:
                    detail = ("该行写入已被首个约束错误拦下；它引用的父行在计划中虽存在，"
                              "但父行自身也未能写入，引用闭合仍无法建立。")
                    affects = "需要先让父表条目通过写入，外键结论才可能转为通过。"
                gaps.append({
                    "code": "masked_foreign_key", "table": failure["table"], "key": failure["key"],
                    "linked_uid": failure["uid"], "detail": detail, "affects": affects,
                    "foreign_key": fk,
                })

        verified = sorted(self.verified, key=_verified_sort)
        unchecked.sort(key=lambda rec: (rec["table"] or "~", _key_token(rec["key"])))
        for gap in gaps:
            gap.setdefault("uid", self._uid())
        gaps.sort(key=lambda rec: (rec["table"] or "~", _key_token(rec["key"]), rec["code"],
                                   canonical((rec.get("foreign_key") or {}).get("columns"))))

        v_ids = {rec["uid"]: f"V{index}" for index, rec in enumerate(verified, 1)}
        u_ids = {rec["uid"]: f"U{index}" for index, rec in enumerate(unchecked, 1)}
        g_ids = {rec["uid"]: f"G{index}" for index, rec in enumerate(gaps, 1)}

        v_list = [_public(rec, v_ids[rec["uid"]]) for rec in verified]
        u_list = []
        for rec in unchecked:
            public = _public(rec, u_ids[rec["uid"]])
            blocked_uid = rec.pop("blocked_uid", None)
            public["blocked_by"] = v_ids.get(blocked_uid)
            u_list.append(public)
        g_list = []
        for rec in gaps:
            public = _public(rec, g_ids[rec["uid"]])
            linked_uid = rec.pop("linked_uid", None)
            public["linked_to"] = v_ids.get(linked_uid)
            fk = public.get("foreign_key")
            public["action"] = _gap_action(rec["code"], fk)
            g_list.append(public)

        items = v_list + u_list + g_list
        return {
            "verified": v_list,
            "unchecked": u_list,
            "gaps": g_list,
            "items": items,
            "counts": {"verified": len(v_list), "unchecked": len(u_list), "gaps": len(g_list)},
        }

    def primary_problem(self, blockers):
        inserts = [rec for rec in blockers["verified"] if rec["source"] == "insert"]
        if inserts:
            first = inserts[0]
            return first["code"], first["detail"]
        integrity = [rec for rec in blockers["verified"] if rec["kind"] == "integrity"]
        if integrity:
            return "integrity", integrity[0]["detail"]
        if self.fk_legacy is not None:
            return "foreign_key", f"外键检查失败：{self.fk_legacy}"
        conversion = [rec for rec in blockers["verified"] if rec["kind"] == "row_conversion"]
        if conversion:
            return "row_conversion", conversion[0]["detail"]
        apply_errors = [rec for rec in blockers["verified"] if rec["source"] == "apply"]
        if apply_errors:
            return apply_errors[0]["code"], apply_errors[0]["detail"]
        return "constraint", "重演被约束问题阻断。"


def _gap_action(code, fk):
    if code == "masked_foreign_key" and fk:
        ref = fk["references"]["table"]
        if fk.get("target_state") == "parent_blocked":
            return f"先修复 {ref} 父行自身的阻断，再回到本条目复核外键。"
        return f"在裁决中恢复 {ref} 的对应父行，或让 {'.'.join(fk['columns'])} 改指存在的键。"
    if code == "foreign_key_truncated":
        return "按表分批核对外键，或先修复已列出的违规后重新重演以展开后续条目。"
    return "补齐该检查所需条件后重新完整重演。"


def _key_token(key):
    return canonical(key) if key is not None else "~"


def _verified_sort(rec):
    return (rec["table"] or "~", _key_token(rec["key"]),
            0 if rec["source"] == "insert" else 1, rec["kind"], rec["message"])


def _public(record, public_id):
    clean = {key: value for key, value in record.items() if not key.endswith("_uid") and key != "uid"}
    clean["id"] = public_id
    # 让 id 排在最前只是可读性处理；dict 顺序不影响 canonical 存储。
    return {"id": public_id, **clean}


# -- PRAGMA foreign_key_check 的行身份定位 ----------------------------------

def resolve_rowid_key(db, table, rowid):
    if table is None or not table["key"]:
        return None
    columns = ",".join(ident(column) for column in table["key"])
    row = db.execute(
        f"SELECT {columns} FROM main.{ident(table['name'])} WHERE rowid=?", (rowid,)).fetchone()
    return [key_atom(value) for value in row] if row else None


def collect_foreign_keys(db, report, limit=FK_LIMIT):
    cursor = db.execute("PRAGMA main.foreign_key_check")
    raw = cursor.fetchmany(limit)
    extra = cursor.fetchone()
    if not raw:
        return
    report.fk_legacy = "、".join(
        f"{table} → {parent}（约束 {fkid}）" for table, _rowid, parent, fkid in raw)
    without_rowid = set()
    for table_name, rowid, parent, fkid in raw:
        table = report.tables.get(table_name)
        if rowid is None:
            without_rowid.add((table_name, fkid, parent))
            continue
        try:
            key = resolve_rowid_key(db, table, rowid)
        except sqlite3.Error:
            key = None
        report.foreign_key_finding(table_name, key, parent, fkid)
    for table_name, fkid, parent in sorted(without_rowid):
        try:
            keys = _anti_join_keys_db(db, report, table_name, fkid, limit)
        except sqlite3.Error:
            keys = []
        if keys:
            for key in keys:
                report.foreign_key_finding(table_name, key, parent, fkid)
        else:
            # 反查失败时仍保留一条没有行键的已验证 pragma 结果。
            report.foreign_key_finding(table_name, None, parent, fkid)
    if extra is not None:
        report.foreign_key_truncated(limit)


def _anti_join_keys_db(db, report, table_name, fkid, limit):
    table = report.tables.get(table_name)
    if table is None:
        return []
    group = next((group for group in group_foreign_keys(table) if group["id"] == fkid), None)
    if group is None:
        return []
    parent_table = report.tables.get(group["parent"])
    match_pairs = []
    for index, (frm, to) in enumerate(group["pairs"]):
        if to is None:
            if parent_table is None or index >= len(parent_table["key"]):
                return []
            to = parent_table["key"][index]
        match_pairs.append((frm, to))
    key_columns = ",".join(f"c.{ident(column)}" for column in table["key"])
    conditions = " AND ".join(f"c.{ident(frm)} IS NOT NULL" for frm, _ in match_pairs)
    match = " AND ".join(
        f"p.{ident(to)} = c.{ident(frm)}" for frm, to in match_pairs)
    sql = (f"SELECT {key_columns} FROM main.{ident(table['name'])} c WHERE {conditions} "
           f"AND NOT EXISTS (SELECT 1 FROM main.{ident(group['parent'])} p WHERE {match}) "
           f"LIMIT ?")
    rows = db.execute(sql, (limit,)).fetchall()
    return [[key_atom(value) for value in row] for row in rows]
