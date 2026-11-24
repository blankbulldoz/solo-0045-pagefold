"""已封存合流的离线逆向包。

发放方凭祖本镜像、封存候选与封存凭证签发逆向包；接收方脱离服务，
仅凭包文件与目标库完成只读预检、候选依赖分析和原子还原。
逆向包只覆盖本次合流确实修改的行：交付后被对方改写的行保持原样，
已经是祖本值的行幂等跳过，与合流无关的行从不触碰。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .catalog import describe
from .codec import atom, canonical, digest, fingerprint, unpack
from .constraints import check_database
from .errors import Problem
from .rows import read_image
from .sqlite_image import ident, mounted, validate_header

FORMAT = "pagefold-reverse-v1"

STATUS_LABEL = {"revert": "待还原", "already_base": "已是祖本值", "conflict": "交付后被改写"}
ACTION_LABEL = {"update": "改回祖本值", "insert": "恢复祖本行", "delete": "删除合流新增行", "none": "不动"}


def _show(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ── 签发 ──

def _digest(package):
    return fingerprint([FORMAT, package["certificate"], package["schema_signature"], package["entries"]])


def issue(base_data, candidate_data, certificate):
    """从祖本镜像、封存候选与凭证签发逆向包，全程不接触服务状态。"""
    validate_header(base_data)
    validate_header(candidate_data)
    if digest(candidate_data) != certificate.get("candidate_digest"):
        raise Problem("candidate_digest", "候选镜像与封存凭证的摘要不一致。")
    sources = certificate.get("sources") or {}
    if digest(base_data) != sources.get("base"):
        raise Problem("base_digest", "祖本镜像与封存凭证的来源摘要不一致。")
    with mounted(candidate_data) as db:
        catalog = describe(db)
    if certificate.get("schema") not in (None, catalog["signature"]):
        raise Problem("schema_mismatch", "候选镜像的模式与封存凭证不一致。")
    with mounted(base_data) as db:
        ancestor = read_image(db, catalog)
    with mounted(candidate_data) as db:
        merged = read_image(db, catalog)
    entries = []
    for table in catalog["tables"]:
        name = table["name"]
        for token in sorted(set(ancestor[name]) | set(merged[name])):
            base_row, merged_row = ancestor[name].get(token), merged[name].get(token)
            if base_row != merged_row:
                entries.append({"id": fingerprint([name, json.loads(token)]), "table": name,
                                "key": json.loads(token), "base": base_row, "merged": merged_row})
    package = {"format": FORMAT,
               "certificate": {"plan": certificate.get("plan"), "revision": certificate.get("revision"),
                               "candidate_digest": certificate.get("candidate_digest"), "sources": sources},
               "schema_signature": catalog["signature"], "tables": catalog["tables"], "entries": entries}
    package["digest"] = _digest(package)
    return package


def dump_package(package):
    return canonical(package)


def load_package(data):
    try:
        package = json.loads(data)
    except json.JSONDecodeError as exc:
        raise Problem("package_format", f"逆向包不是有效的 JSON：{exc}") from exc
    if not isinstance(package, dict) or package.get("format") != FORMAT:
        raise Problem("package_format", "文件不是 pagefold 逆向包。")
    if package.get("digest") != _digest(package):
        raise Problem("package_digest", "逆向包摘要校验失败，内容可能被篡改。")
    return package


# ── 预检 ──

def _classify(entry, current_row):
    """三态区分：仍是合流结果（待还原）、已是祖本值、交付后被改写。"""
    if current_row == entry["merged"]:
        return "revert"
    if current_row == entry["base"]:
        return "already_base"
    return "conflict"


def _action(entry):
    if entry["merged"] is None:
        return "insert"
    if entry["base"] is None:
        return "delete"
    return "update"


def _fk_groups(table):
    groups = {}
    for row in table["foreign_keys"]:
        group = groups.setdefault(row[0], {"table": row[2], "from": [], "to": []})
        group["from"].append(row[3])
        group["to"].append(row[4])
    return list(groups.values())


def _columns(row, table, names):
    return [row[table["columns"].index(name)] for name in names]


def _referenced(row, table, fk, target):
    to_columns = [fk["to"][k] or target["key"][k] for k in range(len(fk["from"]))]
    return _columns(row, table, to_columns)


def _dependencies(package, tables, states, current):
    """候选间的先后边与包外阻塞。

    删除行被包内其他候选的最终态引用时形成先后边；被包外行（或不再
    变动的候选行）引用时构成阻塞。插入、更新行的外键目标若只能由包内
    其他候选提供，同样形成先后边；最终态仍缺失则构成阻塞。
    """
    final = {name: dict(rows) for name, rows in current.items()}
    for state in states:
        if state["status"] != "revert":
            continue
        entry = state["entry"]
        token = canonical(entry["key"])
        if entry["base"] is None:
            final[entry["table"]].pop(token, None)
        else:
            final[entry["table"]][token] = entry["base"]
    position = {(state["entry"]["table"], canonical(state["entry"]["key"])): index
                for index, state in enumerate(states)}
    edges, blockers = [], []
    for index, state in enumerate(states):
        entry = state["entry"]
        if state["status"] != "revert":
            continue
        table = tables[entry["table"]]
        if entry["base"] is None:
            for source in package["tables"]:
                for fk in _fk_groups(source):
                    if fk["table"] != entry["table"]:
                        continue
                    wanted = _referenced(entry["merged"], table, fk, table)
                    for token, row in current[source["name"]].items():
                        if _columns(row, source, fk["from"]) != wanted:
                            continue
                        referrer = {"table": source["name"], "key": json.loads(token),
                                    "via": f"{source['name']}({','.join(fk['from'])})"}
                        other = position.get((source["name"], token))
                        final_row = final[source["name"]].get(token)
                        moved = other is not None and states[other]["status"] == "revert" and (
                            final_row is None or _columns(final_row, source, fk["from"]) != wanted)
                        if moved:
                            edges.append((other, index, referrer["via"]))
                        else:
                            blockers.append({"entry": entry["id"], "kind": "referenced",
                                             "referrer": referrer,
                                             "detail": "待删行被包外行引用"})
        else:
            for fk in _fk_groups(table):
                wanted = _columns(entry["base"], table, fk["from"])
                if any(part[0] == "n" for part in wanted):
                    continue
                target = tables[fk["table"]]
                hit = next((token for token, row in final[fk["table"]].items()
                            if _referenced(row, target, fk, target) == wanted), None)
                if hit is None:
                    blockers.append({"entry": entry["id"], "kind": "missing_target",
                                     "target": {"table": fk["table"], "via":
                                                f"{table['name']}({','.join(fk['from'])})"},
                                     "detail": "还原后的外键目标在最终态缺失"})
                elif hit not in current[fk["table"]]:
                    other = position.get((fk["table"], hit))
                    if other is not None:
                        edges.append((other, index, f"{table['name']}({','.join(fk['from'])})"))
    return edges, blockers


def _batches(count, edges):
    incoming = [set() for _ in range(count)]
    outgoing = [set() for _ in range(count)]
    for before, after, _via in edges:
        incoming[after].add(before)
        outgoing[before].add(after)
    ready = sorted(index for index in range(count) if not incoming[index])
    batches, done = [], 0
    while ready:
        batches.append(ready)
        done += len(ready)
        follow = []
        for index in ready:
            for other in outgoing[index]:
                incoming[other].discard(index)
                if not incoming[other]:
                    follow.append(other)
        ready = sorted(follow)
    if done != count:
        raise Problem("dependency_cycle", "逆向候选之间存在循环依赖，无法分批。")
    return batches


def preflight(package, target_data):
    """只读预检：逐行三态分类、候选依赖分批、整份可行性。不写目标库。"""
    validate_header(target_data)
    with mounted(target_data) as db:
        catalog = describe(db)
        if catalog["signature"] != package["schema_signature"]:
            raise Problem("schema_drift", "目标库模式已漂移，与逆向包封存的模式不一致。", 409)
        current = read_image(db, catalog)
    tables = {table["name"]: table for table in package["tables"]}
    entries = package["entries"]
    states = []
    for entry in entries:
        row = current[entry["table"]].get(canonical(entry["key"]))
        states.append({"entry": entry, "status": _classify(entry, row), "current": row})
    edges, blockers = _dependencies(package, tables, states, current)
    revert_order = [index for index, state in enumerate(states) if state["status"] == "revert"]
    order = {index: n for n, index in enumerate(revert_order)}
    edge_list = [(order[before], order[after], via) for before, after, via in edges]
    batches = [[entries[revert_order[index]]["id"] for index in batch]
               for batch in _batches(len(revert_order), edge_list)]
    report_entries = []
    for state in states:
        entry = state["entry"]
        item = {"id": entry["id"], "table": entry["table"], "key": entry["key"],
                "status": state["status"],
                "action": _action(entry) if state["status"] == "revert" else "none"}
        if state["status"] == "conflict":
            item["current"] = state["current"]
        report_entries.append(item)
    summary = {status: sum(state["status"] == status for state in states)
               for status in ("revert", "already_base", "conflict")}
    return {"format": "pagefold-reverse-report-v1", "package_digest": package["digest"],
            "plan": package["certificate"].get("plan"), "target_digest": digest(target_data),
            "feasible": not blockers, "summary": summary, "entries": report_entries,
            "dependencies": [{"before": entries[revert_order[before]]["id"],
                              "after": entries[revert_order[after]]["id"], "via": via}
                             for before, after, via in edge_list],
            "batches": batches, "blockers": blockers}


# ── 应用 ──

def _fetch_row(db, table, key):
    columns = ",".join(map(ident, table["columns"]))
    predicate = " AND ".join(f"{ident(column)} COLLATE BINARY=?" for column in table["key"])
    row = db.execute(f"SELECT {columns} FROM main.{ident(table['name'])} WHERE {predicate}",
                     tuple(map(unpack, key))).fetchone()
    return [atom(value) for value in row] if row is not None else None


def _execute(db, table, entry):
    name = ident(table["name"])
    predicate = " AND ".join(f"{ident(column)} COLLATE BINARY=?" for column in table["key"])
    if entry["merged"] is not None:
        db.execute(f"DELETE FROM main.{name} WHERE {predicate}", tuple(map(unpack, entry["key"])))
    if entry["base"] is not None:
        columns = ",".join(map(ident, table["columns"]))
        params = ",".join("?" for _ in table["columns"])
        try:
            db.execute(f"INSERT OR ABORT INTO main.{name} ({columns}) VALUES ({params})",
                       tuple(map(unpack, entry["base"])))
        except sqlite3.IntegrityError as exc:
            raise Problem("constraint", f"{table['name']} {canonical(entry['key'])}：{exc}") from exc


def _rehearse(package, data, ordered_states):
    """在内存副本上完整执行并验证，失败时目标文件从未被打开。"""
    tables = {table["name"]: table for table in package["tables"]}
    catalog = {"signature": package["schema_signature"], "tables": package["tables"]}
    with mounted(data, writable=True) as db:
        expected = read_image(db, catalog)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for state in ordered_states:
            entry = state["entry"]
            _execute(db, tables[entry["table"]], entry)
            token = canonical(entry["key"])
            if entry["base"] is None:
                expected[entry["table"]].pop(token, None)
            else:
                expected[entry["table"]][token] = entry["base"]
        check_database(db)
        if read_image(db, catalog) != expected:
            raise Problem("row_conversion", "写入后读取到的行与还原目标不一致。")
        db.rollback()


def _commit(package, target_path, ordered_states):
    """落盘事务：前提复核、执行、约束验证，任一失败整体回滚。"""
    tables = {table["name"]: table for table in package["tables"]}
    db = sqlite3.connect(target_path, isolation_level=None)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        for state in ordered_states:
            entry = state["entry"]
            table = tables[entry["table"]]
            if _fetch_row(db, table, entry["key"]) != entry["merged"]:
                raise Problem("concurrent_drift",
                              f"{entry['table']} {canonical(entry['key'])} 在应用期间被改写，已整体回滚。", 409)
            _execute(db, table, entry)
        check_database(db)
        db.commit()
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def apply(package, target_path, strict=False):
    """原子应用逆向包。任何失败路径都不改动目标库。"""
    target_path = Path(target_path)
    report = preflight(package, target_path.read_bytes())
    report["strict"] = strict
    report["applied"] = False
    conflicts = [entry for entry in report["entries"] if entry["status"] == "conflict"]
    if strict and conflicts:
        report["error"] = {"code": "conflict",
                           "detail": f"{len(conflicts)} 行在交付后被改写，严格模式拒绝应用。"}
        return report
    if not report["feasible"]:
        report["error"] = {"code": "blocked", "detail": "存在包外引用或缺失的外键目标，整份不可应用。"}
        return report
    states = []
    for entry in package["entries"]:
        status = next(item["status"] for item in report["entries"] if item["id"] == entry["id"])
        states.append({"entry": entry, "status": status})
    by_id = {state["entry"]["id"]: state for state in states}
    ordered = [by_id[entry_id] for batch in report["batches"] for entry_id in batch]
    try:
        _rehearse(package, target_path.read_bytes(), ordered)
        _commit(package, target_path, ordered)
    except Problem as exc:
        report["error"] = exc.as_dict()
        return report
    except sqlite3.Error as exc:
        report["error"] = {"code": "sqlite_apply", "detail": str(exc)}
        return report
    report["applied"] = True
    report["reverted"] = [state["entry"]["id"] for state in ordered]
    report["protected"] = [entry["id"] for entry in conflicts]
    return report


# ── 报告与命令行 ──

def render_report(report):
    summary = report["summary"]
    lines = [f"逆向包 {report['package_digest'][:16]}…（计划 {report.get('plan')}）",
             f"目标摘要 {report['target_digest'][:16]}…  "
             f"整份可行性：{'可以应用' if report['feasible'] else '不可应用'}",
             f"待还原 {summary['revert']} 行，已是祖本 {summary['already_base']} 行，"
             f"交付后被改写 {summary['conflict']} 行"]
    for entry in report["entries"]:
        line = f"  {entry['table']} {_show(entry['key'])}：{STATUS_LABEL[entry['status']]}"
        if entry["action"] != "none":
            line += f"（{ACTION_LABEL[entry['action']]}）"
        if entry["status"] == "conflict":
            line += f"，当前值 {_show(entry['current'])}"
        lines.append(line)
    if report["dependencies"]:
        lines.append("候选间依赖（先 → 后）：")
        for edge in report["dependencies"]:
            lines.append(f"  {edge['before'][:12]}… → {edge['after'][:12]}…（{edge['via']}）")
    if report["batches"]:
        lines.append("分批计划：" + "；".join(f"第 {n + 1} 批 {len(batch)} 项"
                                              for n, batch in enumerate(report["batches"])))
    for blocker in report["blockers"]:
        if blocker["kind"] == "referenced":
            referrer = blocker["referrer"]
            lines.append(f"阻塞：{blocker['detail']}（{referrer['table']} 键 "
                         f"{_show(referrer['key'])}，经 {referrer['via']}）")
        else:
            lines.append(f"阻塞：{blocker['detail']}（{blocker['target']['table']}）")
    if "error" in report:
        lines.append(f"失败：{report['error']['code']} {report['error']['detail']}")
    if report.get("applied"):
        lines.append("已原子应用。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pagefold.reverse", description="已封存合流的离线逆向包工具")
    commands = parser.add_subparsers(dest="command", required=True)
    issue_cmd = commands.add_parser("issue", help="从祖本、封存候选与凭证签发逆向包")
    issue_cmd.add_argument("base", type=Path)
    issue_cmd.add_argument("candidate", type=Path)
    issue_cmd.add_argument("certificate", type=Path)
    issue_cmd.add_argument("-o", "--output", type=Path, required=True)
    for name, text in (("preflight", "只读预检目标库并输出分批计划"), ("apply", "原子应用逆向包")):
        command = commands.add_parser(name, help=text)
        command.add_argument("package", type=Path)
        command.add_argument("target", type=Path)
        command.add_argument("--json", action="store_true")
        if name == "apply":
            command.add_argument("--strict", action="store_true", help="交付后被改写的行也视为失败")
    args = parser.parse_args(argv)
    try:
        if args.command == "issue":
            package = issue(args.base.read_bytes(), args.candidate.read_bytes(),
                            json.loads(args.certificate.read_text(encoding="utf-8")))
            args.output.write_text(dump_package(package), encoding="utf-8")
            print(f"逆向包已签发：{args.output}（{len(package['entries'])} 条，"
                  f"摘要 {package['digest'][:16]}…）")
            return 0
        package = load_package(args.package.read_text(encoding="utf-8"))
        if args.command == "preflight":
            report = preflight(package, args.target.read_bytes())
        else:
            report = apply(package, args.target, strict=args.strict)
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_report(report))
        return 0 if report.get("applied", report["feasible"]) else 1
    except Problem as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
