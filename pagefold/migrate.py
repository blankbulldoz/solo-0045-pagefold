"""把内嵌二进制的旧结构状态库（v1）迁移为内容寻址的新结构（v2）。

v1 在 images.body、attempts.candidate、releases.candidate 三处保存二进制；
v2 的状态库只保存元数据，不可变文件按 SHA-256 摘要在 <状态库>.blobs/ 中
单独存放，同一份内容跨来源、跨成果只保存一次。

迁移可续执行：复制阶段按批提交，每行先把内容写入内容库（幂等），再在
*_migrating 影子表中登记元数据。中断只会留下"文件已写、状态未换"的
现场，重新运行时已写的文件被直接复用、已登记的行被跳过。全部复制并
预校验通过后，才在单个事务内完成表切换，因此旧结构始终可用到最后一刻。

    python3 -m pagefold.migrate run ./pagefold-state.sqlite
    python3 -m pagefold.migrate verify ./pagefold-state.sqlite
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from .blobs import DIGEST, BlobStore, blob_path
from .codec import digest, fingerprint
from .store import SCHEMA_VERSION, schema_version

SHADOW = {
    "images": """
        CREATE TABLE IF NOT EXISTS images_migrating (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, catalog TEXT NOT NULL, size INTEGER NOT NULL
        )""",
    "attempts": """
        CREATE TABLE IF NOT EXISTS attempts_migrating (
            plan_id TEXT NOT NULL REFERENCES plans(id), revision INTEGER NOT NULL,
            result TEXT NOT NULL, candidate_digest TEXT, PRIMARY KEY(plan_id, revision)
        )""",
    "releases": """
        CREATE TABLE IF NOT EXISTS releases_migrating (
            plan_id TEXT PRIMARY KEY REFERENCES plans(id), certificate TEXT NOT NULL,
            candidate_digest TEXT NOT NULL
        )""",
}


class MigrationError(Exception):
    pass


def _shadow_present(db):
    return any(row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_migrating'"))


def _connect(state):
    db = sqlite3.connect(state, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def _copy_rows(db, blobs, table, keys, rows, batch, stop, log):
    """逐行把二进制写入内容库并登记影子表，按批提交；全部幂等可重入。

    已登记的行直接跳过（其内容此前必然已写入内容库），因此中断后
    重新运行只做尚未完成的部分。
    """
    existing = {tuple(row) if len(row) > 1 else row[0]
                for row in db.execute(f"SELECT {keys} FROM {table}_migrating")}
    copied = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        for key, params, data, expect in rows:
            if key in existing:
                continue
            if data is not None:
                written = blobs.put(data)
                if expect is not None and written != expect:
                    raise MigrationError(
                        f"{table} 记录标识 {expect} 与实际内容摘要 {written} 不一致，迁移中止。")
                stop["puts"] += 1
                if stop["limit"] is not None and stop["puts"] >= stop["limit"]:
                    db.execute("ROLLBACK")
                    log(f"模拟中断：已写 {stop['puts']} 份文件，当前批次未提交（文件已写、状态未换）。")
                    os._exit(3)
            db.execute(f"INSERT OR IGNORE INTO {table}_migrating VALUES({','.join('?' * len(params))})",
                       params)
            copied += 1
            if copied % batch == 0:
                db.execute("COMMIT")
                db.execute("BEGIN IMMEDIATE")
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return copied


def _copy_images(db, blobs, batch, stop, log):
    rows = [(row["id"], (row["id"], row["label"], row["catalog"], len(row["body"])), row["body"],
             row["id"]) for row in db.execute("SELECT id,label,catalog,body FROM images ORDER BY rowid")]
    return _copy_rows(db, blobs, "images", "id", rows, batch, stop, log)


def _copy_attempts(db, blobs, batch, stop, log):
    rows = []
    for row in db.execute("SELECT plan_id,revision,result,candidate FROM attempts ORDER BY rowid"):
        data = row["candidate"]
        params = (row["plan_id"], row["revision"], row["result"], digest(data) if data is not None else None)
        rows.append(((row["plan_id"], row["revision"]), params, data, None))
    return _copy_rows(db, blobs, "attempts", "plan_id,revision", rows, batch, stop, log)


def _copy_releases(db, blobs, batch, stop, log):
    rows = []
    for row in db.execute("SELECT plan_id,certificate,candidate FROM releases ORDER BY rowid"):
        data = row["candidate"]
        rows.append((row["plan_id"], (row["plan_id"], row["certificate"], digest(data)), data, None))
    return _copy_rows(db, blobs, "releases", "plan_id", rows, batch, stop, log)


def _finalize(db):
    """单事务完成表切换：旧表下线、影子表转正、写入结构版本。"""
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES('schema_version',?)", (SCHEMA_VERSION,))
        for table in ("images", "attempts", "releases"):
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_migrating RENAME TO {table}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def _hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(db, blobs, suffix=""):
    """逐项核对状态库元数据与内容库文件的字节一致性，返回报告。"""
    report = {"images": 0, "attempts": 0, "releases": 0, "plans": 0,
              "references": 0, "blobs": 0, "problems": [], "warnings": []}
    problems, warnings = report["problems"], report["warnings"]

    def blob_check(owner, expected, size):
        if not DIGEST.fullmatch(expected or ""):
            problems.append(f"{owner}：摘要格式无效")
            return
        path = blobs.root / expected
        if not path.is_file():
            problems.append(f"{owner}：内容库缺少文件 {expected[:12]}…")
            return
        actual = _hash_file(path)
        if actual != expected:
            problems.append(f"{owner}：文件摘要 {actual[:12]}… 与记录 {expected[:12]}… 不一致")
        elif size is not None and path.stat().st_size != size:
            problems.append(f"{owner}：文件长度 {path.stat().st_size} 与记录 {size} 不一致")

    images = {}
    for row in db.execute(f"SELECT id,label,catalog,size FROM images{suffix}"):
        report["images"] += 1
        report["references"] += 1
        try:
            images[row["id"]] = json.loads(row["catalog"])
        except (ValueError, TypeError):
            problems.append(f"镜像 {row['id'][:12]}…：目录记录无法解析")
            continue
        blob_check(f"镜像 {row['id'][:12]}…", row["id"], row["size"])

    plans = {}
    for row in db.execute("SELECT id,base,left_id,right_id,items,decisions,revision,state FROM plans"):
        report["plans"] += 1
        plans[row["id"]] = row
        for role in ("base", "left_id", "right_id"):
            if row[role] not in images:
                problems.append(f"计划 {row['id'][:12]}…：{role} 引用的镜像 {row[role][:12]}… 不存在")

    attempts = {}
    for row in db.execute(f"SELECT plan_id,revision,result,candidate_digest FROM attempts{suffix}"):
        report["attempts"] += 1
        owner = f"重演 {row['plan_id'][:12]}…@{row['revision']}"
        try:
            result = json.loads(row["result"])
        except (ValueError, TypeError):
            problems.append(f"{owner}：结果记录无法解析")
            continue
        if result.get("id") != row["plan_id"] or result.get("revision") != row["revision"]:
            problems.append(f"{owner}：结果记录与主键不一致")
        if row["plan_id"] not in plans:
            problems.append(f"{owner}：所属计划不存在")
        if result.get("status") == "passed":
            if row["candidate_digest"] is None:
                problems.append(f"{owner}：已通过但缺少候选摘要")
            elif row["candidate_digest"] != result.get("candidate_digest"):
                problems.append(f"{owner}：候选摘要与结果记录不一致")
            else:
                report["references"] += 1
                blob_check(owner, row["candidate_digest"], result.get("size"))
        elif row["candidate_digest"] is not None:
            problems.append(f"{owner}：未通过却保留了候选文件")
        attempts[(row["plan_id"], row["revision"])] = row["candidate_digest"]

    for row in db.execute(f"SELECT plan_id,certificate,candidate_digest FROM releases{suffix}"):
        report["releases"] += 1
        report["references"] += 1
        owner = f"凭证 {row['plan_id'][:12]}…"
        try:
            certificate = json.loads(row["certificate"])
        except (ValueError, TypeError):
            problems.append(f"{owner}：凭证无法解析")
            continue
        plan = plans.get(row["plan_id"])
        if certificate.get("format") != "pagefold-certificate-v1":
            problems.append(f"{owner}：凭证格式未知")
        if certificate.get("plan") != row["plan_id"]:
            problems.append(f"{owner}：凭证与计划主键不一致")
        if row["candidate_digest"] != certificate.get("candidate_digest"):
            problems.append(f"{owner}：候选摘要与凭证不一致")
        blob_check(owner, row["candidate_digest"], certificate.get("candidate_size"))
        if plan is None:
            problems.append(f"{owner}：所属计划不存在")
            continue
        if plan["state"] not in ("sealed", "archived"):
            problems.append(f"{owner}：计划状态为 {plan['state']}，不应存在凭证")
        if certificate.get("revision") != plan["revision"]:
            problems.append(f"{owner}：凭证版本 {certificate.get('revision')} 与计划版本 {plan['revision']} 不一致")
        sources = certificate.get("sources") or {}
        if sources.get("base") != plan["base"] or sources.get("left") != plan["left_id"] \
                or sources.get("right") != plan["right_id"]:
            problems.append(f"{owner}：凭证来源与计划不一致")
        base_catalog = images.get(plan["base"])
        if base_catalog is not None and certificate.get("schema") != base_catalog.get("signature"):
            problems.append(f"{owner}：凭证模式摘要与祖本目录不一致")
        try:
            if certificate.get("plan_digest") != fingerprint(json.loads(plan["items"])):
                problems.append(f"{owner}：凭证计划摘要与计划内容不一致")
            if certificate.get("decision_digest") != fingerprint(json.loads(plan["decisions"])):
                problems.append(f"{owner}：凭证裁决摘要与裁决记录不一致")
        except (ValueError, TypeError):
            problems.append(f"{owner}：计划内容无法解析")
        if attempts.get((row["plan_id"], plan["revision"])) != row["candidate_digest"]:
            problems.append(f"{owner}：与对应重演记录的候选摘要不一致")

    referenced = set(images)
    referenced.update(d for d in attempts.values() if d)
    referenced.update(row[0] for row in db.execute(f"SELECT candidate_digest FROM releases{suffix}"))
    for name in blobs.files():
        report["blobs"] += 1
        if name not in referenced:
            warnings.append(f"内容库文件 {name[:12]}… 未被任何记录引用")
    for name in blobs.stray():
        warnings.append(f"内容库残留临时文件 {name}")
    return report


def verify_state(state):
    state = Path(state)
    if not state.is_file():
        raise MigrationError(f"状态库 {state} 不存在。")
    db = _connect(state)
    try:
        version = schema_version(db)
        if version != SCHEMA_VERSION or _shadow_present(db):
            raise MigrationError(f"状态库 {state} 尚未完成迁移（结构版本 {version}）。")
        report = verify(db, BlobStore(blob_path(state)))
    finally:
        db.close()
    return report


def run(state, batch=64, stop_after=None, vacuum=True, log=print):
    state = Path(state)
    if not state.is_file():
        raise MigrationError(f"状态库 {state} 不存在。")
    blobs = BlobStore(blob_path(state))
    db = _connect(state)
    try:
        version = schema_version(db)
        if version == SCHEMA_VERSION and not _shadow_present(db):
            log("状态库已是元数据加内容库的新结构，无需迁移。")
            return verify(db, blobs)
        if version != "1":
            raise MigrationError(f"状态库结构版本 {version} 无法识别，迁移中止。")
        if _shadow_present(db):
            log("检测到上次迁移的现场（影子表已存在），继续未完成的复制。")
        for ddl in SHADOW.values():
            db.execute(ddl)
        stop = {"limit": stop_after, "puts": 0}
        for table, copy in (("images", _copy_images), ("attempts", _copy_attempts),
                            ("releases", _copy_releases)):
            copied = copy(db, blobs, batch, stop, log)
            log(f"{table}: 已登记 {copied} 行。")
        report = verify(db, blobs, suffix="_migrating")
        if report["problems"]:
            raise MigrationError("预校验未通过，保留旧结构现场：\n" + "\n".join(report["problems"]))
        _finalize(db)
        log("表切换完成：状态库已转为元数据结构。")
        report = verify(db, blobs)
        if report["problems"]:
            raise MigrationError("迁移后校验未通过：\n" + "\n".join(report["problems"]))
        if vacuum:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log("已回收旧二进制占用的页面。")
        return report
    finally:
        db.close()


def _print_report(report, out):
    saved = report["references"] - report.get("blobs", 0)
    out(f"镜像 {report['images']}、重演 {report['attempts']}、凭证 {report['releases']}、"
        f"计划 {report['plans']}；内容库文件 {report.get('blobs', 0)} 份"
        f"（引用 {report['references']} 次，去重 {max(saved, 0)} 份）。")
    for warning in report["warnings"]:
        out(f"警告：{warning}")
    for problem in report["problems"]:
        out(f"问题：{problem}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pagefold.migrate", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="执行可续迁移（完成后自动校验）")
    run_parser.add_argument("state", type=Path)
    run_parser.add_argument("--batch", type=int, default=64, help="每批提交的行数")
    run_parser.add_argument("--stop-after", type=int, default=None,
                            help="演练用：写入指定份数文件后模拟中断（未提交的批次回滚）")
    run_parser.add_argument("--no-vacuum", action="store_true", help="跳过迁移后的页面回收")
    verify_parser = commands.add_parser("verify", help="逐项校验历史凭证与内容库字节一致")
    verify_parser.add_argument("state", type=Path)
    verify_parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            report = run(args.state, batch=args.batch, stop_after=args.stop_after,
                         vacuum=not args.no_vacuum)
        else:
            report = verify_state(args.state)
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as exc:
        print(f"状态库无法读取：{exc}", file=sys.stderr)
        return 1
    if args.command == "verify" and args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report, print)
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
