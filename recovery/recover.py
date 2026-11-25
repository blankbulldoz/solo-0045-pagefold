"""按计划隔离恢复：把生产状态库中获准的一份计划复制到独立演练状态库。

设计要点
--------
- 生产库以只读 URI 打开（``mode=ro``、``query_only=ON``），恢复过程对生产零写入。
- 全程持有一个延迟读事务，读取的 images/plans/attempts/releases 四张表来自同一个
  已提交时点；并发写入在事务开始后提交的内容不会混入（WAL 的快照隔离）。
- 目标库先写 ``*.part`` 暂存文件并在单事务内装配，成功后原子 ``os.replace`` 改名；
  中断只会留下暂存文件，不会出现可被误交的半成品实例。
- 只复制获准计划引用到的镜像（按 SHA-256 去重，共享祖本只带一份），其他团队独有的
  镜像、计划、重演记录和封存成果一律不进入演练库。
- 输出文件关闭前切回 rollback journal，保证单个自包含 SQLite 文件，无 -wal/-shm 附属件。
"""

import argparse
import base64
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pagefold.codec import canonical
from pagefold.store import SCHEMA

MANIFEST_VERSION = "pagefold-recovery-manifest-v1"
TABLES = ("images", "plans", "attempts", "releases")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hashable(value):
    if isinstance(value, bytes):
        return {"__blob__": base64.b64encode(value).decode("ascii")}
    return value


def logical_digest(path):
    """规范化逻辑内容哈希：与页布局、journal 模式无关，服务打开前后保持稳定。

    把四张表按固定顺序导出为同一 canonical JSON 再求 SHA-256；BLOB 列用
    base64 包装对象确定性编码。
    """
    db = sqlite3.connect(path, timeout=8)
    db.row_factory = sqlite3.Row
    try:
        payload = {}
        for table in TABLES:
            order = "plan_id" if table == "releases" else "rowid"
            payload[table] = [tuple(_hashable(cell) for cell in row)
                              for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        return hashlib.sha256(canonical(payload).encode("ascii")).hexdigest()
    finally:
        db.close()


def open_readonly(path):
    """以只读方式打开生产库；mode=ro 保证不修改生产文件与 WAL。"""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=8)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def list_plans(source_path):
    """供操作者选择计划：列出生产库中的计划、版本、状态与引用镜像。"""
    with open_readonly(source_path) as db:
        return [dict(row) for row in db.execute("""
            SELECT p.id, p.revision, p.state,
                   p.base, p.left_id, p.right_id,
                   b.label AS base_label, l.label AS left_label, r.label AS right_label
            FROM plans p
            JOIN images b ON b.id = p.base
            JOIN images l ON l.id = p.left_id
            JOIN images r ON r.id = p.right_id
            ORDER BY p.rowid
        """)]


def _select_scoped_rows(source, plan_id):
    """在同一个读事务内取出获准计划的完整闭包，并计算需要排除的内容。"""
    plan = source.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise SystemExit(f"生产库中找不到计划 {plan_id}，未生成任何文件。")

    wanted = [plan["base"], plan["left_id"], plan["right_id"]]
    if len(set(wanted)) != len(wanted):
        # 三方可以引用同一份镜像（如演示中的重复上传），按摘要去重保留。
        wanted = list(dict.fromkeys(wanted))

    placeholders = ",".join("?" for _ in wanted)
    images = source.execute(
        f"SELECT id,label,catalog,body FROM images WHERE id IN ({placeholders}) ORDER BY id",
        wanted).fetchall()
    found = {row["id"] for row in images}
    missing = [image_id for image_id in wanted if image_id not in found]
    if missing:
        raise SystemExit(f"计划引用的镜像在生产库缺失：{missing}，中止恢复。")
    for row in images:
        actual = hashlib.sha256(row["body"]).hexdigest()
        if actual != row["id"]:
            raise SystemExit(f"镜像 {row['id']} 字节摘要与寻址 ID 不符，中止恢复。")

    attempts = source.execute(
        "SELECT plan_id,revision,result,candidate FROM attempts WHERE plan_id=? ORDER BY revision",
        (plan_id,)).fetchall()
    release = source.execute(
        "SELECT plan_id,certificate,candidate FROM releases WHERE plan_id=?",
        (plan_id,)).fetchone()

    all_images = [row[0] for row in source.execute("SELECT id FROM images ORDER BY id")]
    other_plans = [dict(row) for row in source.execute(
        "SELECT id,base,left_id,right_id FROM plans WHERE id<>? ORDER BY rowid", (plan_id,))]
    excluded_images = sorted(set(all_images) - found)
    scoped = sorted(found)

    # 祖本可能被其他计划共享：只有“未被本计划引用”的镜像才算需要排除的他方资产。
    return {
        "plan": plan, "images": images, "attempts": attempts, "release": release,
        "scoped_images": scoped, "excluded_images": excluded_images,
        "other_plans": other_plans,
    }


def _assemble(target_path, scope, crash_point=None):
    """在单事务内把闭包装进暂存库；cr_point 用于演练中断，以硬退出模拟进程被杀。"""
    if target_path.exists():
        target_path.unlink()
    side_files = [target_path.with_suffix(target_path.suffix + suffix)
                  for suffix in ("-wal", "-shm", "-journal")]
    for side in side_files:
        if side.exists():
            side.unlink()

    db = sqlite3.connect(target_path, timeout=8, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        if crash_point == "images":
            db.commit()  # 仅落了空表结构，随后进程直接消失，模拟装配中途被杀死。
            os._exit(86)
        db.executemany("INSERT INTO images(id,label,catalog,body) VALUES(?,?,?,?)",
                       [(row["id"], row["label"], row["catalog"], row["body"])
                        for row in scope["images"]])
        if crash_point == "plan":
            os._exit(86)
        plan = scope["plan"]
        db.execute("INSERT INTO plans(id,base,left_id,right_id,items,decisions,revision,state)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (plan["id"], plan["base"], plan["left_id"], plan["right_id"],
                    plan["items"], plan["decisions"], plan["revision"], plan["state"]))
        db.executemany("INSERT INTO attempts(plan_id,revision,result,candidate) VALUES(?,?,?,?)",
                       [(row["plan_id"], row["revision"], row["result"], row["candidate"])
                        for row in scope["attempts"]])
        if scope["release"] is not None:
            row = scope["release"]
            db.execute("INSERT INTO releases(plan_id,certificate,candidate) VALUES(?,?,?)",
                       (row["plan_id"], row["certificate"], row["candidate"]))
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SystemExit(f"暂存库完整性检查失败：{integrity}")
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit(f"暂存库存在外键违规：{violations}")
        db.commit()
        # 收尾为自包含单文件，避免把 -wal/-shm 当成可交接产物。
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
        for side in side_files:
            if side.exists():
                side.unlink()


def _snapshot_marker(source):
    """记录读事务开始时的时点标记：journal 模式与各表行数/最大 rowid。

    WAL 模式下 BEGIN 已为当前连接固定读快照；这里的标记用于清单留痕和事后比对，
    真正的隔离保证来自单读事务 + 只读连接。
    """
    return {
        "journal_mode": source.execute("PRAGMA journal_mode").fetchone()[0],
        "plan_count": source.execute("SELECT count(*) FROM plans").fetchone()[0],
        "image_count": source.execute("SELECT count(*) FROM images").fetchone()[0],
        "max_image_rowid": source.execute(
            "SELECT coalesce(max(rowid),0) FROM images").fetchone()[0],
        "max_plan_rowid": source.execute(
            "SELECT coalesce(max(rowid),0) FROM plans").fetchone()[0],
    }


def build_manifest(source_path, target_path, scope, source_snapshot, end_marker):
    plan = scope["plan"]
    return {
        "format": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "database": str(Path(source_path).resolve()),
            "snapshot_marker_at_begin": source_snapshot,
            "snapshot_marker_at_end": end_marker,
            "stable_throughout": source_snapshot == end_marker,
            "image_count_total": len(scope["scoped_images"]) + len(scope["excluded_images"]),
            "plan_count_total": 1 + len(scope["other_plans"]),
        },
        "selected_plan": {
            "id": plan["id"],
            "revision": plan["revision"],
            "state": plan["state"],
            "sources": {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
            "plan_row_sha256": hashlib.sha256(canonical({
                "id": plan["id"], "base": plan["base"], "left_id": plan["left_id"],
                "right_id": plan["right_id"], "items": json.loads(plan["items"]),
                "decisions": json.loads(plan["decisions"]),
                "revision": plan["revision"], "state": plan["state"]}).encode()).hexdigest(),
        },
        "included_images": [
            {"id": row["id"], "label": row["label"], "body_sha256": hashlib.sha256(row["body"]).hexdigest()}
            for row in scope["images"]],
        "attempt_revisions": [row["revision"] for row in scope["attempts"]],
        "release_present": scope["release"] is not None,
        "excluded": {
            "plan_ids": [row["id"] for row in scope["other_plans"]],
            "image_ids": scope["excluded_images"],
        },
        "drill_database": {
            "path": str(Path(target_path).resolve()),
            "file_sha256": sha256_file(target_path),
            "logical_sha256": logical_digest(target_path),
            "bytes": target_path.stat().st_size,
            "journal_mode": "delete",
        },
    }


def recover(source_path, target_path, plan_id=None, manifest_path=None, crash_point=None):
    """执行一次按计划隔离恢复，返回清单字典。"""
    source_path, target_path = Path(source_path), Path(target_path)
    if not source_path.exists():
        raise SystemExit(f"生产状态库不存在：{source_path}")
    staging = target_path.with_name(target_path.name + ".part")
    if staging.exists():
        # 上次中断的暂存件不是可交接实例，重跑前清除。
        staging.unlink()

    with open_readonly(source_path) as source:
        source.execute("BEGIN")  # 延迟读事务：以下所有读取共享同一已提交时点快照
        if plan_id is None:
            ids = [row[0] for row in source.execute("SELECT id FROM plans ORDER BY rowid")]
            if len(ids) != 1:
                raise SystemExit("未指定计划且生产库中计划数量不为 1，请用 --plan 显式选择。")
            plan_id = ids[0]
        source_snapshot = _snapshot_marker(source)
        scope = _select_scoped_rows(source, plan_id)
        _assemble(staging, scope, crash_point=crash_point)
        # 暂存库装配完成后仍在同一快照内再读一次计数：
        # 若期间生产库有并发提交，这里应与事务开始时完全相同，证明读到的是同一时点。
        end_marker = _snapshot_marker(source)
        source.rollback()

    os.replace(staging, target_path)  # 原子改名：目标路径要么不存在，要么是完整库
    manifest = build_manifest(source_path, target_path, scope, source_snapshot, end_marker)
    if manifest_path is None:
        manifest_path = target_path.with_suffix(".manifest.json")
    manifest_path = Path(manifest_path)
    tmp_manifest = manifest_path.with_name(manifest_path.name + ".part")
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_manifest, manifest_path)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="按计划隔离恢复 Pagefold 演练实例")
    parser.add_argument("--source", required=True, type=Path, help="生产状态库路径（只读打开）")
    parser.add_argument("--output", type=Path, help="演练状态库输出路径")
    parser.add_argument("--plan", help="获准恢复的计划 ID；省略时要求生产库仅有一份计划")
    parser.add_argument("--manifest", type=Path, help="恢复清单输出路径（默认与输出同目录）")
    parser.add_argument("--list", action="store_true", help="仅列出生产库中的计划后退出")
    parser.add_argument("--crash-point", choices=("images", "plan"),
                        help="演练用：在指定装配点模拟进程中断")
    args = parser.parse_args()
    if args.list:
        for row in list_plans(args.source):
            print(json.dumps(row, ensure_ascii=False))
        return
    if args.output is None:
        parser.error("恢复模式需要 --output")
    manifest = recover(args.source, args.output, args.plan, args.manifest, args.crash_point)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
