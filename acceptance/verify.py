#!/usr/bin/env python3
"""Pagefold 离线验收入口。

独立实现,只依赖 Python 3.11+ 标准库,不导入 pagefold 包、不访问 Pagefold 服务。
接收方按手边材料的完整程度,逐层核对封存凭证、成果数据库、三份输入与裁决记录:

  层 1 字节匹配    成果的尺寸与 SHA-256 对得上凭证 candidate_size/candidate_digest,
                  且成果是健康独立的 SQLite 镜像(完整性、外键)。
  层 2 来源吻合    三份输入的 SHA-256 分别对得上凭证 sources.base/left/right,
                  各输入是合法镜像,模式签名对得上凭证 schema。
  层 3 实际行重演  由三份输入重算三方计划(对得上 plan_digest),按裁决记录
                  (对得上 decision_digest)重演候选,与凭证 candidate_digest
                  逐字节一致;成果再与重演候选逐行比对。

成果字节与凭证不同(例如同一逻辑成果经过 VACUUM 等 SQLite 整理)时,第 3 层
给出可复核的逻辑比对:模式签名一致且全部表行内容一致,则结论为"内容等价"。

用法:
  python3 -I acceptance/verify.py --pack 验收包目录 [--json 报告路径]
  python3 -I acceptance/verify.py --certificate 凭证.json --deliverable 成果.sqlite \
      [--base 祖本.sqlite --left 左支.sqlite --right 右支.sqlite] \
      [--adjudication 裁决.json] [--json 报告路径]

退出码:0 验收通过;1 不予验收;2 用法或材料读取错误。
"""
import argparse
import base64
import hashlib
import json
import math
import platform
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 20_000
ROLES = ("base", "left", "right")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
CERTIFICATE_FORMAT = "pagefold-certificate-v1"
ADJUDICATION_FORMAT = "pagefold-adjudication-v1"
CERTIFICATE_KEYS = {"format", "plan", "revision", "sources", "schema",
                    "plan_digest", "decision_digest", "candidate_digest", "candidate_size"}


class Reject(Exception):
    """核对过程中发现的不一致或无法读取。"""


class UsageError(Exception):
    """命令行材料缺失或无法读取。"""


# ---------- 规范编码(与凭证摘要规则对应的独立实现) ----------

def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(canonical(value).encode("ascii"))


def atom(value):
    if value is None:
        return ["n"]
    if isinstance(value, int):
        return ["i", str(value)]
    if isinstance(value, float) and math.isfinite(value):
        return ["r", value.hex()]
    if isinstance(value, str):
        return ["t", value]
    if isinstance(value, bytes):
        return ["b", base64.b64encode(value).decode("ascii")]
    raise Reject(f"镜像包含无法保真编码的值:{type(value).__name__}")


def unpack(value):
    if value[0] == "n":
        return None
    decoders = {"i": int, "r": float.fromhex, "t": str, "b": base64.b64decode}
    return decoders[value[0]](value[1])


def key_atom(value):
    if isinstance(value, float) and value.is_integer() and -(2 ** 63) <= value < 2 ** 63:
        return atom(int(value))
    return atom(value)


# ---------- SQLite 镜像 ----------

def validate_header(data):
    if len(data) > MAX_BYTES:
        raise Reject("镜像超过 4 MiB 上限")
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        raise Reject("文件不是完整的 SQLite 镜像")
    size = int.from_bytes(data[16:18], "big") or 0
    if size == 1:
        size = 65536
    if size < 512 or size > 65536 or size & (size - 1) or len(data) % size:
        raise Reject("镜像的页大小或文件长度无效")
    if data[18:20] != b"\x01\x01":
        raise Reject("镜像未完成检查点,不是独立的 SQLite 文件")


@contextmanager
def mounted(data, writable=False):
    validate_header(data)
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.deserialize(data)
        db.execute("PRAGMA trusted_schema=OFF")
        if not writable:
            db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def ident(name):
    return '"' + name.replace('"', '""') + '"'


def check_database(db):
    integrity = db.execute("PRAGMA main.integrity_check(1)").fetchone()[0]
    if integrity != "ok":
        raise Reject(f"SQLite 完整性检查失败:{integrity}")
    if db.execute("PRAGMA main.foreign_key_check").fetchmany(12):
        raise Reject("外键检查失败")


# ---------- 目录(模式签名) ----------

def relation(db, name):
    fields = db.execute(f"PRAGMA main.table_xinfo({ident(name)})").fetchall()
    position = sorted((field[5], field[1]) for field in fields if field[5])
    if not position or any(field[6] for field in fields):
        raise Reject(f"表 {name} 需要显式主键,并且所有列须可写入")
    for _, index_name, _, origin, _ in db.execute(f"PRAGMA main.index_list({ident(name)})"):
        if origin != "pk":
            continue
        parts = db.execute(f"PRAGMA main.index_xinfo({ident(index_name)})").fetchall()
        if any(part[5] and part[4].upper() != "BINARY" for part in parts):
            raise Reject(f"表 {name} 的主键须使用 BINARY 比较")
    foreign = db.execute(f"PRAGMA main.foreign_key_list({ident(name)})").fetchall()
    return {"name": name, "columns": [f[1] for f in fields], "types": [f[2] for f in fields],
            "key": [col for _, col in position], "foreign_keys": [list(row) for row in foreign]}


def describe(db):
    schemas = db.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
                         "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name").fetchall()
    listed = {r[1]: r[2] for r in db.execute("PRAGMA main.table_list") if r[0] == "main"}
    if any(kind not in {"table", "index"} or (kind == "table" and listed.get(name) != "table")
           for kind, name, _, _ in schemas):
        raise Reject("镜像只接受普通表和索引")
    tables = [(name, sql) for kind, name, _, sql in schemas if kind == "table"]
    if not 1 <= len(tables) <= 32:
        raise Reject("镜像须包含 1 至 32 张表")
    if db.execute("SELECT 1 FROM main.sqlite_schema WHERE name='sqlite_sequence'").fetchone():
        raise Reject("镜像含有自动递增序列")
    return {"signature": fingerprint(schemas), "tables": [relation(db, name) for name, _ in tables]}


# ---------- 行扫描 ----------

def read_table(db, table, allowance):
    name = table["name"]
    positions = [table["columns"].index(key) for key in table["key"]]
    cols = ",".join(map(ident, table["columns"]))
    result = {}
    cursor = db.execute(f"SELECT {cols} FROM main.{ident(name)} LIMIT ?", (allowance + 1,))
    for raw in cursor:
        if len(result) >= allowance:
            raise Reject("镜像超过 20,000 行上限")
        row = [atom(value) for value in raw]
        key = [key_atom(raw[pos]) for pos in positions]
        if any(part[0] == "n" for part in key):
            raise Reject(f"表 {name} 含有 NULL 主键")
        token = canonical(key)
        if token in result:
            raise Reject(f"表 {name} 含有重复主键")
        result[token] = row
    return result


def read_image(db, catalog):
    result = {}
    remaining = MAX_ROWS
    for table in catalog["tables"]:
        rows = read_table(db, table, remaining)
        result[table["name"]] = rows
        remaining -= len(rows)
    return result


# ---------- 三方计划与裁决 ----------

def calculate(base, left, right):
    result = []
    for table in sorted(base):
        versions = [snapshot[table] for snapshot in (base, left, right)]
        keys = sorted(set().union(*versions))
        for key in keys:
            ancestor, lrow, rrow = (values.get(key) for values in versions)
            if lrow == rrow == ancestor:
                continue
            choice = "left" if lrow == rrow or rrow == ancestor else "right" if lrow == ancestor else None
            item_key = json.loads(key)
            result.append({"id": fingerprint([table, item_key]), "table": table, "key": item_key,
                           "base": ancestor, "left": lrow, "right": rrow, "automatic": choice})
    return result


def selected(item, decisions):
    role = item["automatic"] or decisions.get(item["id"])
    return (role, item[role] if role else None)


# ---------- 重演 ----------

def amendments(items, decisions):
    changed = {}
    for item in items:
        role, row = selected(item, decisions)
        if role is None:
            raise Reject("存在未裁决的行")
        if row != item["base"]:
            changed.setdefault(item["table"], []).append((item, row))
    return changed


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
                db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})",
                           tuple(map(unpack, row)))
            except sqlite3.IntegrityError as exc:
                raise Reject(f"{table['name']} {canonical(item['key'])}:{exc}")


def rebuild(ancestor, catalog, operations):
    with mounted(ancestor, writable=True) as db:
        expected = read_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for table in catalog["tables"]:
            name = table["name"]
            changes = operations.get(name, [])
            apply_rows(db, table, changes)
            for item, row in changes:
                key = canonical(item["key"])
                if row is None:
                    expected[name].pop(key, None)
                else:
                    expected[name][key] = row
        check_database(db)
        if read_image(db, catalog) != expected:
            raise Reject("写入后读取到的行与计划目标不一致")
        db.commit()
        data = db.serialize()
        validate_header(data)
        return data


# ---------- 报告 ----------

class Layer:
    def __init__(self, number, name, available=True, reason=None):
        self.data = {"layer": number, "name": name, "available": available, "checks": []}
        if not available:
            self.data["reason"] = reason
        self.failed = []

    def check(self, cid, ok, **detail):
        entry = {"id": cid, "ok": bool(ok)}
        entry.update(detail)
        self.data["checks"].append(entry)
        if not ok:
            self.failed.append(entry)
        return ok

    @property
    def ok(self):
        return not self.failed

    def done(self):
        if self.data["available"]:
            self.data["ok"] = self.ok
        return self.data


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"{what}无法读取:{exc}")


def certificate_problems(certificate):
    if not isinstance(certificate, dict):
        return ["凭证须为 JSON 对象"]
    problems = []
    missing = CERTIFICATE_KEYS - set(certificate)
    extra = set(certificate) - CERTIFICATE_KEYS
    if missing:
        problems.append("缺少字段:" + ",".join(sorted(missing)))
    if extra:
        problems.append("含未知字段:" + ",".join(sorted(extra)))
    if certificate.get("format") != CERTIFICATE_FORMAT:
        problems.append(f"format 须为 {CERTIFICATE_FORMAT}")
    plan = certificate.get("plan")
    if not (isinstance(plan, str) and HEX32.fullmatch(plan)):
        problems.append("plan 须为 32 位十六进制计划标识")
    revision = certificate.get("revision")
    if not (isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0):
        problems.append("revision 须为非负整数")
    sources = certificate.get("sources")
    if not (isinstance(sources, dict) and set(sources) == set(ROLES)
            and all(isinstance(sources.get(role), str) and HEX64.fullmatch(sources[role]) for role in ROLES)):
        problems.append("sources 须恰好包含 base/left/right 三个 64 位十六进制摘要")
    for field in ("schema", "plan_digest", "decision_digest", "candidate_digest"):
        value = certificate.get(field)
        if not (isinstance(value, str) and HEX64.fullmatch(value)):
            problems.append(f"{field} 须为 64 位十六进制摘要")
    size = certificate.get("candidate_size")
    if not (isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_BYTES):
        problems.append("candidate_size 须为 0 至 4 MiB 的整数")
    return problems


def adjudication_problems(adjudication):
    if not isinstance(adjudication, dict):
        return ["裁决记录须为 JSON 对象"]
    problems = []
    if adjudication.get("format") != ADJUDICATION_FORMAT:
        problems.append(f"format 须为 {ADJUDICATION_FORMAT}")
    decisions = adjudication.get("decisions")
    if not (isinstance(decisions, dict)
            and all(isinstance(key, str) and HEX64.fullmatch(key) and choice in ROLES
                    for key, choice in decisions.items())):
        problems.append("decisions 须为 条目摘要 → base/left/right 的映射")
    return problems


def compare_rows(delivered, replayed, certificate):
    """成果与重演候选的逻辑比对,给出可复核的逐表结果。"""
    comparison = {"deliverable_digest": digest(delivered), "replayed_digest": digest(replayed),
                  "certificate_candidate_digest": certificate.get("candidate_digest"),
                  "byte_identical": digest(delivered) == digest(replayed)}
    try:
        with mounted(delivered) as db:
            delivered_catalog = describe(db)
            delivered_rows = read_image(db, delivered_catalog)
    except (Reject, sqlite3.Error) as exc:
        comparison["schema"] = {"certificate": certificate.get("schema"), "equal": False, "error": str(exc)}
        return comparison, False, f"成果无法按目录读取:{exc}"
    with mounted(replayed) as db:
        replayed_catalog = describe(db)
        replayed_rows = read_image(db, replayed_catalog)
    schema_equal = (delivered_catalog["signature"] == replayed_catalog["signature"]
                    == certificate.get("schema"))
    comparison["schema"] = {"certificate": certificate.get("schema"),
                            "deliverable": delivered_catalog["signature"],
                            "replayed": replayed_catalog["signature"], "equal": schema_equal}
    tables = []
    differences = []
    names = [table["name"] for table in replayed_catalog["tables"]]
    for name in names:
        d_rows = delivered_rows.get(name)
        r_rows = replayed_rows[name]
        equal = d_rows == r_rows
        tables.append({"table": name,
                       "delivered_rows": len(d_rows) if d_rows is not None else None,
                       "replayed_rows": len(r_rows),
                       "delivered_digest": fingerprint(d_rows) if d_rows is not None else None,
                       "replayed_digest": fingerprint(r_rows), "equal": equal})
        if not equal and d_rows is not None:
            for token in sorted(set(d_rows) | set(r_rows)):
                if d_rows.get(token) != r_rows.get(token):
                    differences.append({"table": name, "key": json.loads(token),
                                        "delivered": d_rows.get(token), "replayed": r_rows.get(token)})
    for name in sorted(set(delivered_rows) - set(names)):
        tables.append({"table": name, "delivered_rows": len(delivered_rows[name]),
                       "replayed_rows": None, "delivered_digest": fingerprint(delivered_rows[name]),
                       "replayed_digest": None, "equal": False})
        differences.append({"table": name, "key": None, "delivered": "整表多出", "replayed": None})
    content_equal = schema_equal and not differences and all(t["equal"] for t in tables)
    comparison["tables"] = tables
    comparison["row_content_equal"] = content_equal
    comparison["difference_count"] = len(differences)
    comparison["differences"] = differences[:20]
    if content_equal:
        detail = f"模式签名一致,{len(tables)} 张表行内容逐行一致"
    else:
        detail = f"发现 {len(differences)} 处行差异" + ("" if schema_equal else ",且模式签名不一致")
    return comparison, content_equal, detail


# ---------- 分层核对 ----------

def verify(certificate_path, deliverable_path, input_paths, adjudication_path):
    report = {"format": "pagefold-acceptance-report-v1",
              "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "tool": {"entry": "acceptance/verify.py",
                       "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
              "materials": {"certificate": str(certificate_path), "deliverable": str(deliverable_path),
                            "inputs": {role: str(input_paths[role]) if input_paths.get(role) else None
                                       for role in ROLES},
                            "adjudication": str(adjudication_path) if adjudication_path else None},
              "layers": [], "comparison": None, "verdict": None}

    certificate = load_json(certificate_path, "封存凭证")
    try:
        deliverable = Path(deliverable_path).read_bytes()
    except OSError as exc:
        raise UsageError(f"成果数据库无法读取:{exc}")
    sources = certificate.get("sources") if isinstance(certificate.get("sources"), dict) else {}

    # 层 1 字节匹配
    layer1 = Layer(1, "字节匹配")
    problems = certificate_problems(certificate)
    structure_ok = layer1.check("certificate.structure", not problems,
                                detail=";".join(problems) or "格式、字段与摘要形状全部合法")
    layer1.check("deliverable.size", len(deliverable) == certificate.get("candidate_size"),
                 actual=len(deliverable), expected=certificate.get("candidate_size"))
    deliverable_digest = digest(deliverable)
    layer1.check("deliverable.digest", deliverable_digest == certificate.get("candidate_digest"),
                 actual=deliverable_digest, expected=certificate.get("candidate_digest"))
    try:
        with mounted(deliverable) as db:
            check_database(db)
        image_ok = layer1.check("deliverable.image", True, detail="镜像头合法,完整性与外键检查通过")
    except (Reject, sqlite3.Error) as exc:
        image_ok = layer1.check("deliverable.image", False, detail=str(exc))
    byte_ok = all(entry["ok"] for entry in layer1.data["checks"]
                  if entry["id"] in {"deliverable.size", "deliverable.digest"})

    # 层 2 来源吻合
    have_inputs = all(input_paths.get(role) for role in ROLES)
    if have_inputs:
        layer2 = Layer(2, "来源吻合")
        input_bytes = {}
        for role in ROLES:
            try:
                input_bytes[role] = Path(input_paths[role]).read_bytes()
            except OSError as exc:
                raise UsageError(f"输入镜像无法读取:{exc}")
            layer2.check(f"inputs.{role}.digest", digest(input_bytes[role]) == sources.get(role),
                         actual=digest(input_bytes[role]), expected=sources.get(role))
            try:
                with mounted(input_bytes[role]) as db:
                    check_database(db)
                layer2.check(f"inputs.{role}.image", True, detail="镜像头合法,完整性与外键检查通过")
            except (Reject, sqlite3.Error) as exc:
                layer2.check(f"inputs.{role}.image", False, detail=str(exc))
            try:
                with mounted(input_bytes[role]) as db:
                    signature = describe(db)["signature"]
                layer2.check(f"inputs.{role}.schema", signature == certificate.get("schema"),
                             actual=signature, expected=certificate.get("schema"))
            except (Reject, sqlite3.Error) as exc:
                layer2.check(f"inputs.{role}.schema", False, detail=str(exc))
    else:
        missing = ",".join(role for role in ROLES if not input_paths.get(role))
        layer2 = Layer(2, "来源吻合", available=False, reason=f"未提供三份输入(缺 {missing})")
        input_bytes = {}

    # 层 3 实际行重演
    if have_inputs and adjudication_path:
        layer3 = Layer(3, "实际行重演")
        adjudication = load_json(adjudication_path, "裁决记录")
        problems = adjudication_problems(adjudication)
        layer3.check("adjudication.structure", not problems,
                     detail=";".join(problems) or "格式与裁决映射合法")
        decisions = adjudication.get("decisions") if isinstance(adjudication.get("decisions"), dict) else {}
        layer3.check("adjudication.binding",
                     adjudication.get("plan") == certificate.get("plan")
                     and adjudication.get("revision") == certificate.get("revision"),
                     detail=f"裁决绑定计划 {adjudication.get('plan')} 版本 {adjudication.get('revision')}")
        try:
            with mounted(input_bytes["base"]) as db:
                catalog = describe(db)
            snapshots = {}
            for role in ROLES:
                with mounted(input_bytes[role]) as db:
                    snapshots[role] = read_image(db, catalog)
            items = calculate(snapshots["base"], snapshots["left"], snapshots["right"])
        except (Reject, sqlite3.Error) as exc:
            items = None
            layer3.check("plan.digest", False, detail=f"无法由输入重算计划:{exc}")
        if items is not None:
            layer3.check("plan.digest", fingerprint(items) == certificate.get("plan_digest"),
                         recomputed=fingerprint(items), expected=certificate.get("plan_digest"),
                         items=len(items))
            conflicts = {item["id"] for item in items if item["automatic"] is None}
            extra = sorted(set(decisions) - conflicts)
            missing = sorted(conflicts - set(decisions))
            layer3.check("adjudication.coverage", not extra and not missing,
                         detail=(f"冲突 {len(conflicts)} 项,裁决 {len(decisions)} 条,一一对应"
                                 if not extra and not missing else
                                 f"多余裁决 {extra},未裁决冲突 {missing}"))
            layer3.check("decisions.digest", fingerprint(decisions) == certificate.get("decision_digest"),
                         recomputed=fingerprint(decisions), expected=certificate.get("decision_digest"))
            try:
                operations = amendments(items, decisions)
                replayed = rebuild(input_bytes["base"], catalog, operations)
                layer3.check("replay.rebuild", True,
                             detail=f"重演通过约束、完整性与重读校验,变更 {sum(map(len, operations.values()))} 行")
            except (Reject, sqlite3.Error) as exc:
                replayed = None
                layer3.check("replay.rebuild", False, detail=str(exc))
            if replayed is not None:
                layer3.check("replay.digest",
                             digest(replayed) == certificate.get("candidate_digest")
                             and len(replayed) == certificate.get("candidate_size"),
                             recomputed=digest(replayed), expected=certificate.get("candidate_digest"),
                             size=len(replayed))
                comparison, logical_ok, detail = compare_rows(deliverable, replayed, certificate)
                report["comparison"] = comparison
                layer3.check("deliverable.logical", logical_ok, detail=detail)
    else:
        reason = "未提供裁决记录" if have_inputs else "重演需要三份输入与裁决记录"
        layer3 = Layer(3, "实际行重演", available=False, reason=reason)

    report["layers"] = [layer1.done(), layer2.done(), layer3.done()]
    report["verdict"] = conclude(layer1, layer2, layer3, structure_ok, byte_ok, image_ok)
    return report


def describe_check(check):
    """把一条检查记录渲染成可读的核对依据。"""
    if check.get("ok") and check.get("detail"):
        return check["detail"]
    parts = []
    if check.get("detail"):
        parts.append(check["detail"])
    parts.extend(f"{key}={check[key]}" for key in ("expected", "actual", "recomputed") if key in check)
    return ",".join(parts) or json.dumps(check, ensure_ascii=False)


def conclude(layer1, layer2, layer3, structure_ok, byte_ok, image_ok):
    def first_failure():
        for layer in (layer1, layer2, layer3):
            if layer.data["available"] and layer.failed:
                return layer.failed[0]
        return None

    def rejected(summary):
        return {"accepted": False, "level": "rejected", "label": "不予验收",
                "layers_verified": [layer.data["name"] for layer in (layer1, layer2, layer3)
                                    if layer.data["available"] and layer.ok],
                "summary": summary}

    def accepted(level, label, summary):
        return {"accepted": True, "level": level, "label": label,
                "layers_verified": [layer.data["name"] for layer in (layer1, layer2, layer3)
                                    if layer.data["available"] and layer.ok],
                "summary": summary}

    if not structure_ok:
        return rejected("凭证结构无效,无法作为验收依据。")
    if layer3.data["available"]:
        if layer3.ok and layer2.ok and image_ok:
            if byte_ok:
                return accepted("row-replay", "实际行重演",
                                "成果字节匹配凭证;来源吻合;由三份输入与裁决记录重演的候选"
                                "与凭证 candidate_digest 逐字节一致。")
            return accepted("logical-equal", "内容等价",
                            "成果字节与凭证 candidate_digest 不同;来源吻合;由输入与裁决重演的"
                            "候选与凭证一致,且成果与重演候选模式签名相同、逐表行内容相同"
                            "(同一逻辑成果,如经 SQLite 整理)。")
        failure = first_failure()
        return rejected(f"第 {failure_layer(layer1, layer2, layer3, failure)} 层检查 "
                        f"{failure['id']} 未通过:{describe_check(failure)}")
    if layer2.data["available"]:
        if layer2.ok and layer1.ok:
            return accepted("source-match", "来源吻合",
                            "成果字节匹配凭证;三份输入摘要与凭证 sources 一致、模式签名与凭证 "
                            "schema 一致;未提供裁决记录,未做重演。")
        failure = first_failure()
        return rejected(f"检查 {failure['id']} 未通过:{describe_check(failure)}")
    if layer1.ok:
        return accepted("byte-match", "字节匹配",
                        "成果字节与凭证 candidate_digest/candidate_size 一致;手边只有凭证与成果,"
                        "来源与裁决未核对。")
    failure = first_failure()
    return rejected(f"检查 {failure['id']} 未通过:{describe_check(failure)}")


def failure_layer(layer1, layer2, layer3, failure):
    for layer in (layer1, layer2, layer3):
        if failure in layer.data["checks"]:
            return layer.data["layer"]
    return "?"


def human_summary(report):
    lines = []
    for layer in report["layers"]:
        if not layer["available"]:
            lines.append(f"层 {layer['layer']} {layer['name']}:— {layer['reason']}")
            continue
        total = len(layer["checks"])
        passed = sum(check["ok"] for check in layer["checks"])
        mark = "✓" if layer["ok"] else "✗"
        lines.append(f"层 {layer['layer']} {layer['name']}:{mark} {passed}/{total}")
        for check in layer["checks"]:
            if not check["ok"]:
                lines.append(f"    ✗ {check['id']}:{describe_check(check)}")
    verdict = report["verdict"]
    headline = f"验收通过 — {verdict['label']}" if verdict["accepted"] else "不予验收"
    lines.append(f"结论:{headline}")
    lines.append(f"  {verdict['summary']}")
    comparison = report.get("comparison")
    if comparison:
        state = "字节一致" if comparison["byte_identical"] else "字节不同"
        tables = comparison.get("tables", [])
        equal = sum(table["equal"] for table in tables)
        lines.append(f"  逻辑比对:成果与重演候选{state};逐表一致 {equal}/{len(tables)}")
    return "\n".join(lines)


def resolve_materials(args):
    certificate = deliverable = adjudication = None
    inputs = {role: None for role in ROLES}
    if args.pack:
        root = Path(args.pack)
        if not root.is_dir():
            raise UsageError(f"验收包目录不存在:{root}")
        certificate = root / "certificate.json"
        if (root / "deliverable.sqlite").is_file():
            deliverable = root / "deliverable.sqlite"
        for role in ROLES:
            candidate = root / "inputs" / f"{role}.sqlite"
            if candidate.is_file():
                inputs[role] = candidate
        if (root / "adjudication.json").is_file():
            adjudication = root / "adjudication.json"
    if args.certificate:
        certificate = Path(args.certificate)
    if args.deliverable:
        deliverable = Path(args.deliverable)
    for role in ROLES:
        value = getattr(args, role)
        if value:
            inputs[role] = Path(value)
    if args.adjudication:
        adjudication = Path(args.adjudication)
    if certificate is None or not Path(certificate).is_file():
        raise UsageError("缺少封存凭证(certificate.json)")
    if deliverable is None or not Path(deliverable).is_file():
        raise UsageError("缺少成果数据库(deliverable.sqlite);验收至少需要凭证与成果")
    for role, path in inputs.items():
        if path is not None and not Path(path).is_file():
            raise UsageError(f"输入镜像不存在:{path}")
    if adjudication is not None and not Path(adjudication).is_file():
        raise UsageError(f"裁决记录不存在:{adjudication}")
    return {"certificate_path": Path(certificate), "deliverable_path": Path(deliverable),
            "input_paths": inputs, "adjudication_path": Path(adjudication) if adjudication else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pagefold 离线验收入口(不依赖 Pagefold 服务)")
    parser.add_argument("--pack", help="验收包目录(自动识别凭证、成果、inputs/ 与裁决记录)")
    parser.add_argument("--certificate", help="封存凭证 JSON")
    parser.add_argument("--deliverable", help="成果 SQLite 数据库")
    parser.add_argument("--base", help="祖本输入镜像")
    parser.add_argument("--left", help="左支输入镜像")
    parser.add_argument("--right", help="右支输入镜像")
    parser.add_argument("--adjudication", help="裁决记录 JSON")
    parser.add_argument("--json", dest="json_path", help="把验收报告(JSON)写入该路径")
    args = parser.parse_args(argv)
    try:
        report = verify(**resolve_materials(args))
    except UsageError as exc:
        print(f"材料错误:{exc}", file=sys.stderr)
        return 2
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")
    print(human_summary(report))
    return 0 if report["verdict"]["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())
