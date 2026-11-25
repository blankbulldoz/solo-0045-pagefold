"""离线核对 Pagefold 封存成果。

接收方只需要本脚本、封存凭证 JSON 和候选 SQLite 文件。若同时提供三份来源镜像，
脚本会独立重算计划摘要、从候选反推裁决并重算裁决摘要，再逐行重建目标数据库，
因此不必信任签发方的状态库也能判断候选是否确为这三份来源在给定裁决下的合流结果。
提供 --state 时，额外以只读方式核对签发方的封存记录、重演记录与版本。

脚本只读取传入的文件，不写回、不修改任何输入。

用法：

    python3 examples/verify_release.py \
        --certificate handover/sealed.json \
        --candidate handover/sealed.sqlite \
        --base handover/quartz.sqlite \
        --left handover/cerulean.sqlite \
        --right handover/umber.sqlite \
        --state handover/state.sqlite

退出码：0 全部通过；1 有核对失败；2 参数或文件无法读取。
"""

import argparse
import base64
import hashlib
import json
import math
import sqlite3
import sys
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 20_000
MAX_TABLES = 32
FORMAT = "pagefold-certificate-v1"
ROLES = ("base", "left", "right")
ROLE_LABELS = {"base": "祖本", "left": "左支", "right": "右支"}
HEX = set("0123456789abcdef")


class Report:
    def __init__(self):
        self.failed = 0
        self.passed = 0
        self.warned = 0

    def ok(self, label, detail=""):
        self.passed += 1
        print(f"通过  {label}" + (f"：{detail}" if detail else ""))

    def fail(self, label, detail):
        self.failed += 1
        print(f"失败  {label}：{detail}")

    def warn(self, label, detail):
        self.warned += 1
        print(f"注意  {label}：{detail}")

    def summary(self):
        print(f"\n核对结束：{self.passed} 项通过，{self.failed} 项失败，{self.warned} 项注意。")
        if self.failed:
            print("结论：不能作为可交付成果接收。")
        else:
            print("结论：文件、凭证与已核对的范围一致。")
        print("范围说明：本脚本不验证签发方的身份与密钥；凭证本身没有密码学签名。"
              "来源绑定、计划绑定和裁决绑定只有在逐项通过后才成立，"
              "而“由谁签发”仍取决于你取得文件的可信渠道或状态库副本。")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return sha256_bytes(canonical(value).encode("ascii"))


def is_digest(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX


def atom(value):
    """与 pagefold.codec.atom 相同的类型保真编码。"""
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
    return ["?", repr(value)]


def key_atom(value):
    """与 pagefold.rows.key_atom 一致：整数值实数与整数视为同一行身份。"""
    if isinstance(value, float) and value.is_integer() and -(2**63) <= value < 2**63:
        return atom(int(value))
    return atom(value)


def quote_ident(name):
    return '"' + name.replace('"', '""') + '"'


def open_readonly(path):
    """只读打开 SQLite：mode=ro 不会创建或改写文件。"""
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=8)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        return db
    except Exception:
        db.close()
        raise


def check_header(report, data):
    if len(data) > MAX_BYTES:
        report.fail("候选大小上限", f"{len(data)} 字节 > {MAX_BYTES} 字节")
        return False
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        report.fail("SQLite 文件头", "不是完整的 SQLite 镜像")
        return False
    page = int.from_bytes(data[16:18], "big") or 0
    if page == 1:
        page = 65536
    if page < 512 or page > 65536 or page & (page - 1) or len(data) % page:
        report.fail("SQLite 页大小", f"页大小 {page} 或文件长度无效")
        return False
    if data[18:20] != b"\x01\x01":
        report.fail("SQLite 日志模式", "文件不是已完成检查点的独立镜像")
        return False
    report.ok("SQLite 文件头与页布局", f"{len(data)} 字节，页大小 {page}")
    return True


def describe(db):
    """按 pagefold.catalog 的规则重建模式签名与表结构，用于独立重算计划。"""
    schemas = [list(row) for row in db.execute(
        "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
        "WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name")]
    if any(kind not in {"table", "index"} for kind, _, _, _ in schemas):
        raise ValueError("镜像含有普通表和索引之外的对象")
    tables = []
    for kind, name, _, _ in schemas:
        if kind != "table":
            continue
        fields = db.execute(f"PRAGMA main.table_xinfo({quote_ident(name)})").fetchall()
        position = sorted((field[5], field[1]) for field in fields if field[5])
        if not position or any(field[6] for field in fields):
            raise ValueError(f"表 {name} 没有显式主键或含生成列")
        tables.append({"name": name, "columns": [field[1] for field in fields],
                       "key": [column for _, column in position]})
    if not 1 <= len(tables) <= MAX_TABLES:
        raise ValueError(f"镜像须包含 1 至 {MAX_TABLES} 张表")
    return fingerprint(schemas), tables


def read_table(db, table, allowance=MAX_ROWS):
    columns = table["columns"]
    positions = [columns.index(key) for key in table["key"]]
    select = ",".join(quote_ident(column) for column in columns)
    result = {}
    for raw in db.execute(f"SELECT {select} FROM main.{quote_ident(table['name'])}"):
        if len(result) >= allowance:
            raise ValueError(f"表 {table['name']} 超出 {allowance} 行预算（全库上限 {MAX_ROWS} 行）")
        row = [atom(value) for value in raw]
        key = [key_atom(raw[position]) for position in positions]
        if any(part[0] == "n" for part in key):
            raise ValueError(f"表 {table['name']} 含 NULL 主键")
        token = canonical(key)
        if token in result:
            raise ValueError(f"表 {table['name']} 含重复主键")
        result[token] = row
    return result


def read_image(db, tables):
    result = {}
    remaining = MAX_ROWS
    for table in tables:
        rows = read_table(db, table, remaining)
        result[table["name"]] = rows
        remaining -= len(rows)
    return result


def calculate(base, left, right):
    """与 pagefold.merge.calculate 相同，用于独立重算计划项。"""
    result = []
    for table in sorted(base):
        versions = [snapshot[table] for snapshot in (base, left, right)]
        for key in sorted(set().union(*versions)):
            ancestor, lrow, rrow = (values.get(key) for values in versions)
            if lrow == rrow == ancestor:
                continue
            choice = "left" if lrow == rrow or rrow == ancestor else "right" if lrow == ancestor else None
            item_key = json.loads(key)
            result.append({"id": fingerprint([table, item_key]), "table": table, "key": item_key,
                           "base": ancestor, "left": lrow, "right": rrow, "automatic": choice})
    return result


def check_certificate(report, certificate):
    if certificate.get("format") != FORMAT:
        report.fail("凭证格式", f"期望 {FORMAT}，实际 {certificate.get('format')!r}")
        return False
    required = ("plan", "revision", "sources", "schema", "plan_digest",
                "decision_digest", "candidate_digest", "candidate_size")
    missing = [key for key in required if key not in certificate]
    if missing:
        report.fail("凭证字段", f"缺少 {', '.join(missing)}")
        return False
    if not isinstance(certificate["plan"], str) or not certificate["plan"]:
        report.fail("凭证计划标识", "plan 不是非空字符串")
        return False
    if not isinstance(certificate["revision"], int) or certificate["revision"] < 0:
        report.fail("凭证版本", "revision 不是非负整数")
        return False
    if not isinstance(certificate["candidate_size"], int) or certificate["candidate_size"] < 0:
        report.fail("凭证候选大小", "candidate_size 不是非负整数")
        return False
    sources = certificate["sources"]
    if not isinstance(sources, dict) or set(sources) != set(ROLES):
        report.fail("凭证来源角色", "sources 必须且只能含 base、left、right")
        return False
    for role in ROLES:
        if not is_digest(sources[role]):
            report.fail("凭证来源摘要", f"{role} 不是 64 位十六进制 SHA-256")
            return False
    for key in ("schema", "plan_digest", "decision_digest", "candidate_digest"):
        if not is_digest(certificate[key]):
            report.fail("凭证摘要", f"{key} 不是 64 位十六进制 SHA-256")
            return False
    report.ok("凭证字段与格式", f"计划 {certificate['plan']}，第 {certificate['revision']} 版")
    return True


def check_sources(report, certificate, paths):
    provided = {role: path for role, path in paths.items() if path is not None}
    if not provided:
        report.warn("来源镜像", "未提供来源文件，只能核对候选自身与凭证，无法核对来源角色和合流内容")
        return False
    for role, path in provided.items():
        if not path.is_file():
            report.fail(f"来源 {ROLE_LABELS[role]} 文件", f"找不到 {path}")
            continue
        digest = sha256_file(path)
        expected = certificate["sources"][role]
        if digest == expected:
            report.ok(f"来源 {ROLE_LABELS[role]} 字节摘要", digest)
            continue
        holders = [name for name, value in certificate["sources"].items() if value == digest]
        if holders:
            report.fail(f"来源 {ROLE_LABELS[role]} 角色错误",
                        f"该文件摘要属于凭证中的 {ROLE_LABELS[holders[0]]}，疑似文件混放或左右支互换")
        elif digest == certificate["candidate_digest"]:
            report.fail(f"来源 {ROLE_LABELS[role]} 文件错误", "该文件就是封存候选，不是来源镜像")
        else:
            report.fail(f"来源 {ROLE_LABELS[role]} 摘要不符", f"文件 {digest} 不属于本凭证声明的任何来源")
    if len(provided) != len(ROLES):
        report.warn("来源镜像", "只提供了部分来源，跳过来源绑定与合流内容核对")
        return False
    return all(path.is_file() for path in provided.values())


def check_candidate(report, certificate, path, candidate):
    if candidate is None:
        return
    digest = sha256_bytes(candidate)
    if digest == certificate["candidate_digest"]:
        report.ok("候选摘要", digest)
    else:
        report.fail("候选摘要", f"文件 {digest} ≠ 凭证 {certificate['candidate_digest']}")
    if len(candidate) == certificate["candidate_size"]:
        report.ok("候选大小", f"{len(candidate)} 字节")
    else:
        report.fail("候选大小", f"文件 {len(candidate)} 字节 ≠ 凭证 {certificate['candidate_size']} 字节")
    if not check_header(report, candidate):
        return
    try:
        with open_readonly(path) as db:
            integrity = db.execute("PRAGMA main.integrity_check(1)").fetchone()[0]
            if integrity == "ok":
                report.ok("SQLite integrity_check", "ok")
            else:
                report.fail("SQLite integrity_check", integrity)
            violations = db.execute("PRAGMA main.foreign_key_check").fetchmany(12)
            if violations:
                report.fail("SQLite foreign_key_check",
                            "；".join(f"{row[0]} → {row[2]}（约束 {row[3]}）" for row in violations))
            else:
                report.ok("SQLite foreign_key_check", "无违反")
            signature, tables = describe(db)
            if signature == certificate["schema"]:
                report.ok("模式签名", signature)
            else:
                report.fail("模式签名", f"候选 {signature} ≠ 凭证 {certificate['schema']}")
    except (sqlite3.Error, ValueError) as exc:
        report.fail("SQLite 可独立打开", f"{type(exc).__name__}: {exc}")


def check_merge(report, certificate, source_paths, candidate_path):
    """由三份来源独立重算计划，由候选反推裁决，再逐行重建并比对。"""
    try:
        snapshots = {}
        signature = None
        tables = None
        for role in ROLES:
            with open_readonly(source_paths[role]) as db:
                source_signature, source_tables = describe(db)
                if signature is None:
                    signature, tables = source_signature, source_tables
                if source_signature != certificate["schema"]:
                    report.fail("来源模式签名", f"{ROLE_LABELS[role]} {source_signature} ≠ 凭证 {certificate['schema']}")
                    return
                if source_tables != tables:
                    report.fail("来源结构", f"{ROLE_LABELS[role]} 的目录与祖本不同")
                    return
                snapshots[role] = read_image(db, source_tables)
        report.ok("来源模式签名与结构", "三份来源一致，且与凭证模式摘要相同")

        items = calculate(snapshots["base"], snapshots["left"], snapshots["right"])
        plan_digest = fingerprint(items)
        if plan_digest != certificate["plan_digest"]:
            report.fail("计划摘要重算", f"由三份来源重算得 {plan_digest} ≠ 凭证 {certificate['plan_digest']}")
            return
        conflicts = sum(item["automatic"] is None for item in items)
        report.ok("计划摘要重算", f"{len(items)} 项计划项（{conflicts} 项冲突）与凭证一致")

        with open_readonly(candidate_path) as db:
            actual = read_image(db, tables)
    except (OSError, sqlite3.Error, ValueError) as exc:
        report.fail("独立重算", f"{type(exc).__name__}: {exc}")
        return

    decisions = {}
    issues = []
    for item in items:
        if item["automatic"] is not None:
            continue
        current = actual[item["table"]].get(canonical(item["key"]))
        matches = [role for role in ROLES if item[role] == current]
        if len(matches) != 1:
            issues.append(f"{item['table']} {canonical(item['key'])} 可匹配 {matches or '无'} 个来源选择")
        else:
            decisions[item["id"]] = matches[0]
    if issues:
        report.fail("裁决反推", "；".join(issues[:3]))
        return
    decision_digest = fingerprint(decisions)
    if decision_digest != certificate["decision_digest"]:
        report.fail("裁决摘要重算",
                    f"由候选反推得 {decision_digest} ≠ 凭证 {certificate['decision_digest']}；"
                    "候选中的冲突行不是凭证绑定的那组裁决")
        return
    report.ok("裁决摘要重算", f"{len(decisions)} 项冲突裁决与凭证一致")

    expected = {name: dict(rows) for name, rows in snapshots["base"].items()}
    for item in items:
        role = item["automatic"] or decisions.get(item["id"])
        if role is None:
            report.fail("合流内容", f"{item['table']} {canonical(item['key'])} 没有有效裁决")
            return
        if item[role] == item["base"]:
            continue
        key = canonical(item["key"])
        if item[role] is None:
            expected[item["table"]].pop(key, None)
        else:
            expected[item["table"]][key] = item[role]

    if set(actual) != set(expected):
        report.fail("合流内容", "候选的表集合与来源目录不同")
        return
    differences = []
    for table in expected:
        missing = expected[table].keys() - actual[table].keys()
        extra = actual[table].keys() - expected[table].keys()
        changed = [key for key in expected[table].keys() & actual[table].keys()
                   if expected[table][key] != actual[table][key]]
        if missing or extra or changed:
            differences.append((table, len(missing), len(extra), len(changed)))
    if differences:
        table, missing, extra, changed = differences[0]
        report.fail("合流内容", f"{len(differences)} 张表不符；例如 {table}: 缺 {missing} 行、多 {extra} 行、"
                                 f"值不同 {changed} 行")
    else:
        total = sum(len(rows) for rows in expected.values())
        report.ok("合流内容", f"由祖本、自动项与 {len(decisions)} 项冲突裁决逐行重建，"
                              f"{total} 行与候选完全一致")


def check_state(report, certificate, candidate, state_path):
    try:
        db = open_readonly(state_path)
    except (OSError, sqlite3.Error) as exc:
        report.fail("状态库可读", str(exc))
        return
    try:
        row = db.execute("SELECT base,left_id,right_id,items,decisions,revision,state "
                         "FROM plans WHERE id=?", (certificate["plan"],)).fetchone()
        if row is None:
            report.fail("状态库计划", "找不到凭证指向的计划")
            return
        roles = {"base": row[0], "left": row[1], "right": row[2]}
        if roles == certificate["sources"]:
            report.ok("状态库来源角色", "与凭证一致")
        else:
            report.fail("状态库来源角色", "；".join(
                f"{ROLE_LABELS[role]}={'一致' if roles[role] == certificate['sources'][role] else '不一致'}"
                for role in ROLES))
        if fingerprint(json.loads(row[3])) == certificate["plan_digest"]:
            report.ok("计划摘要", certificate["plan_digest"])
        else:
            report.fail("计划摘要", "状态库计划项与凭证不一致")
        if fingerprint(json.loads(row[4])) == certificate["decision_digest"]:
            report.ok("裁决摘要", certificate["decision_digest"])
        else:
            report.fail("裁决摘要", "状态库裁决与凭证不一致")
        if row[6] not in {"sealed", "archived"}:
            report.fail("计划状态", f"{row[6]!r} 不是已封存或已归档")
        elif row[5] == certificate["revision"]:
            report.ok("计划版本与状态", f"第 {row[5]} 版，{row[6]}")
        else:
            report.fail("计划版本", f"状态库第 {row[5]} 版 ≠ 凭证第 {certificate['revision']} 版")

        release = db.execute("SELECT certificate,candidate FROM releases WHERE plan_id=?",
                             (certificate["plan"],)).fetchone()
        if release is None:
            report.fail("封存记录", "状态库没有 releases 行；该计划可能只重演通过、从未封存")
        else:
            if json.loads(release[0]) == certificate:
                report.ok("封存凭证", "与交接的凭证逐字段一致")
            else:
                report.fail("封存凭证", "状态库中的凭证与交接的凭证不一致")
            if sha256_bytes(release[1]) == certificate["candidate_digest"]:
                report.ok("状态库候选摘要", certificate["candidate_digest"])
            else:
                report.fail("状态库候选摘要", "状态库候选字节与凭证不一致")
            if candidate is not None:
                if release[1] == candidate:
                    report.ok("候选字节", "与状态库保存的字节完全相同")
                else:
                    report.fail("候选字节", "交接的候选与状态库保存的候选字节不同")

        attempt = db.execute("SELECT result FROM attempts WHERE plan_id=? AND revision=?",
                             (certificate["plan"], certificate["revision"])).fetchone()
        if attempt is None:
            report.fail("重演记录", "找不到凭证版本的通过记录")
        else:
            result = json.loads(attempt[0])
            if result.get("status") == "passed" and result.get("candidate_digest") == certificate["candidate_digest"]:
                report.ok("重演记录", f"第 {certificate['revision']} 版通过，摘要与凭证一致")
            else:
                report.fail("重演记录", f"状态 {result.get('status')!r} 或摘要不一致")

        for role in ROLES:
            image = db.execute("SELECT body,catalog FROM images WHERE id=?",
                               (certificate["sources"][role],)).fetchone()
            if image is None:
                report.fail(f"状态库来源 {ROLE_LABELS[role]}", "找不到该摘要的镜像")
            elif sha256_bytes(image[0]) != certificate["sources"][role]:
                report.fail(f"状态库来源 {ROLE_LABELS[role]}", "镜像字节与摘要不符")
            elif json.loads(image[1]).get("signature") != certificate["schema"]:
                report.fail(f"状态库来源 {ROLE_LABELS[role]}", "镜像模式签名与凭证不一致")
            else:
                report.ok(f"状态库来源 {ROLE_LABELS[role]}", certificate["sources"][role])
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        report.fail("状态库核对", f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def map_directory(report, certificate, directory, certificate_path):
    """文件混放时按字节摘要辨认各自角色，不凭文件名。"""
    if not directory.is_dir():
        report.fail("清点目录", f"{directory} 不是目录")
        return
    expected = dict(certificate["sources"])
    expected["candidate"] = certificate["candidate_digest"]
    found = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        try:
            digest = sha256_file(path)
        except OSError as exc:
            report.warn("清点文件", f"{path}: {exc}")
            continue
        role = next((name for name, value in expected.items() if value == digest), None)
        if role is None:
            continue
        if role in found:
            report.warn(f"重复的 {role}", f"{found[role].name} 与 {path.name} 字节相同")
        else:
            found[role] = path
    for role, label in (("base", "祖本"), ("left", "左支"), ("right", "右支"), ("candidate", "候选")):
        if role in found:
            report.ok(f"清点 {label}", str(found[role]))
        else:
            report.warn(f"清点 {label}", "该目录中没有摘要匹配的文件，请向签发方索取")
    if "candidate" in found and all(role in found for role in ROLES):
        print("\n建议命令（按摘要确定角色，不按文件名）：")
        print(f"  python3 -I examples/verify_release.py --certificate {certificate_path} "
              f"--candidate {found['candidate']} --base {found['base']} "
              f"--left {found['left']} --right {found['right']} --state <状态库副本>")


def load_certificate(report, path):
    try:
        certificate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        report.fail("读取凭证", f"{path}: {exc}")
        return None
    if not isinstance(certificate, dict):
        report.fail("读取凭证", "凭证必须是 JSON 对象")
        return None
    return certificate


def read_candidate(report, path):
    try:
        return path.read_bytes()
    except OSError as exc:
        report.fail("读取候选", f"{path}: {exc}")
        return None


def main():
    parser = argparse.ArgumentParser(description="离线核对 Pagefold 封存成果")
    parser.add_argument("--certificate", type=Path, required=True, help="封存凭证 JSON")
    parser.add_argument("--candidate", type=Path, required=True, help="下载的候选 SQLite 文件")
    parser.add_argument("--base", type=Path, help="祖本镜像（建议提供，用于独立重算）")
    parser.add_argument("--left", type=Path, help="左支镜像（建议提供，用于独立重算）")
    parser.add_argument("--right", type=Path, help="右支镜像（建议提供，用于独立重算）")
    parser.add_argument("--state", type=Path, help="签发方状态库的只读副本（可选，用于封存溯源）")
    parser.add_argument("--directory", type=Path, help="交接目录：按摘要辨认各文件角色（可选）")
    args = parser.parse_args()

    report = Report()
    certificate = load_certificate(report, args.certificate)
    if certificate is not None and check_certificate(report, certificate):
        if args.directory is not None:
            map_directory(report, certificate, args.directory, args.certificate)
        source_paths = {"base": args.base, "left": args.left, "right": args.right}
        candidate = read_candidate(report, args.candidate)
        complete_sources = check_sources(report, certificate, source_paths)
        check_candidate(report, certificate, args.candidate, candidate)
        if complete_sources and candidate is not None:
            check_merge(report, certificate, source_paths, args.candidate)
        if args.state is not None:
            check_state(report, certificate, candidate, args.state)
    report.summary()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
