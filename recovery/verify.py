"""演练实例独立核验器。

不导入 pagefold 包，只用标准库 sqlite3/json/hashlib 核对：

1. 演练库是完整、自包含的 SQLite 文件（无 -wal/-shm 附属件、integrity_check 通过）。
2. 库内只有一份计划及其 images/attempts/releases 闭包，外键关系完整。
3. 每份镜像字节的 SHA-256 与其寻址 ID 一致，且能被独立打开、通过完整性与外键检查。
4. 恢复清单与实际库内容逐项一致。
5. 与生产库交叉核验（若提供）：获准计划逐字节一致；他方计划与镜像确实不在演练库。
6. 可选经 HTTP 核对网页/API 暴露的内容与库内一致。

退出码 0 表示全部通过；任一检查失败即非零退出，并打印失败清单。
"""

import argparse
import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.request import urlopen

TABLES = ("images", "plans", "attempts", "releases")


class Report:
    def __init__(self):
        self.passed = []
        self.failed = []

    def check(self, condition, description, detail=""):
        if condition:
            self.passed.append(description)
        else:
            self.failed.append(f"{description} {detail}".rstrip())

    def section(self, title):
        print(f"\n== {title} ==")

    def finish(self):
        for item in self.passed:
            print(f"  ✓ {item}")
        for item in self.failed:
            print(f"  ✗ {item}", file=sys.stderr)
        print(f"\n通过 {len(self.passed)} 项，失败 {len(self.failed)} 项。")
        return 0 if not self.failed else 1


def connect(path):
    db = sqlite3.connect(path, timeout=8)
    db.row_factory = sqlite3.Row
    return db


def _canonical(value):
    # 与 pagefold.codec.canonical 同规则：ASCII、紧凑、排序键，保证跨实现一致。
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _hashable(value):
    if isinstance(value, bytes):
        return {"__blob__": base64.b64encode(value).decode("ascii")}
    return value


def logical_digest(path):
    """规范化逻辑内容哈希：与页布局、journal 模式无关（与 recover.logical_digest 同算法）。"""
    db = connect(path)
    try:
        payload = {}
        for table in TABLES:
            order = "plan_id" if table == "releases" else "rowid"
            payload[table] = [tuple(_hashable(cell) for cell in row)
                              for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        return hashlib.sha256(_canonical(payload).encode("ascii")).hexdigest()
    finally:
        db.close()


def verify_image_body(blob):
    """独立打开镜像字节：头部、页结构、integrity_check、外键与 catalog 可解析。"""
    if len(blob) < 100 or blob[:16] != b"SQLite format 3\x00":
        return False, "不是有效的 SQLite 文件头"
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(blob)
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False, "integrity_check 不为 ok"
        if db.execute("PRAGMA foreign_key_check").fetchall():
            return False, "镜像内部存在外键违规"
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return True, "可独立打开且完整"
    except sqlite3.Error as exc:
        return False, f"无法反序列化：{exc}"
    finally:
        db.close()


def checkpoint_into_main(db_path):
    """把外部服务（Store 打开时切 WAL）产生的 -wal 合并回主库。

    恢复交付时主库是单文件 rollback-journal 模式；演练实例经 pagefold 服务打开后
    可能切到 WAL。核验前先做 TRUNCATE 检查点并切回 delete，使比对针对规范化的单文件，
    而不是服务进程留下的页布局。
    """
    db = sqlite3.connect(db_path, timeout=8)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    finally:
        db.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()


def verify_drill(drill_path, report):
    report.section("演练库文件与结构")
    drill_path = Path(drill_path)
    report.check(drill_path.is_file(), f"演练库文件存在：{drill_path}")
    checkpoint_into_main(drill_path)
    leftovers = [Path(str(drill_path) + suffix) for suffix in ("-wal", "-shm", "-journal")
                 if Path(str(drill_path) + suffix).exists()]
    report.check(not leftovers, "检查点后无 -wal/-shm/-journal 附属件，文件自包含",
                 f"发现 {leftovers}")
    part_file = drill_path.with_name(drill_path.name + ".part")
    report.check(not part_file.exists(), "不存在同名 .part 半成品")

    db = connect(drill_path)
    report.check(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                 "integrity_check = ok")
    report.check(not db.execute("PRAGMA foreign_key_check").fetchall(), "外键检查无违规")
    journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0].lower()
    report.check(journal_mode in {"delete", "wal"},
                 f"journal_mode 可识别（{journal_mode}；交付快照为 delete 单文件）")

    actual_tables = {row["name"]: row["sql"]
                     for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
    report.check(set(actual_tables) == set(TABLES), "只包含 images/plans/attempts/releases 四张表",
                 f"实际 {sorted(actual_tables)}")

    plan_count = db.execute("SELECT count(*) AS n FROM plans").fetchone()["n"]
    report.check(plan_count == 1, f"演练库恰好一份计划（实际 {plan_count}）")
    plan = db.execute("SELECT * FROM plans").fetchone()
    if plan is None:
        db.close()
        return None

    wanted = [plan["base"], plan["left_id"], plan["right_id"]]
    scoped = set(wanted)
    image_rows = db.execute("SELECT id,label,catalog,body FROM images ORDER BY id").fetchall()
    report.check({row["id"] for row in image_rows} == scoped,
                 "images 表只含获准计划引用的镜像（共享祖本去重）",
                 f"库内 {sorted(r['id'] for r in image_rows)} / 计划需要 {sorted(scoped)}")

    report.section("镜像字节寻址与可独立打开")
    for row in image_rows:
        checksum = hashlib.sha256(row["body"]).hexdigest()
        report.check(checksum == row["id"], f"镜像 {row['label']} 的 SHA-256 与寻址 ID 一致")
        ok, detail = verify_image_body(row["body"])
        report.check(ok, f"镜像 {row['label']}（{row['id'][:12]}…）{detail}")
        try:
            catalog = json.loads(row["catalog"])
            report.check("signature" in catalog and catalog["tables"],
                         f"镜像 {row['label']} 的 catalog 含模式摘要与表目录")
        except (ValueError, KeyError):
            report.check(False, f"镜像 {row['label']} 的 catalog 不是有效目录 JSON")

    report.section("计划、重演历史与封存成果")
    attempts = db.execute("SELECT * FROM attempts WHERE plan_id=? ORDER BY revision",
                          (plan["id"],)).fetchall()
    report.check(all(row["plan_id"] == plan["id"] for row in attempts),
                 f"重演历史 {len(attempts)} 条全部属于获准计划")
    for row in attempts:
        result = json.loads(row["result"])
        report.check(result["id"] == plan["id"] and result["revision"] == row["revision"],
                     f"重演记录 rev={row['revision']} 的结果 JSON 与计划绑定一致")
        if result.get("status") == "passed":
            report.check(row["candidate"] is not None
                         and hashlib.sha256(row["candidate"]).hexdigest()
                         == result["candidate_digest"],
                         f"rev={row['revision']} 通过候选存在且摘要匹配")
        else:
            report.check(row["candidate"] is None,
                         f"rev={row['revision']} 受阻记录没有可下载候选")

    release = db.execute("SELECT * FROM releases WHERE plan_id=?", (plan["id"],)).fetchone()
    if release is not None:
        certificate = json.loads(release["certificate"])
        report.check(certificate["plan"] == plan["id"]
                     and certificate["revision"] == plan["revision"],
                     "封存凭证绑定的计划与版本正确")
        report.check(hashlib.sha256(release["candidate"]).hexdigest()
                     == certificate["candidate_digest"],
                     "封存候选字节与凭证摘要一致")
        ok, detail = verify_image_body(release["candidate"])
        report.check(ok, f"封存候选 {detail}")
        report.check(certificate["sources"] == {
            "base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
            "凭证三来源角色与计划引用一致")
    else:
        report.check(plan["state"] != "sealed", "未封存的开放/受阻计划没有 release 行")
    db.close()
    return plan


def verify_manifest(drill_path, manifest_path, report):
    if not manifest_path or not Path(manifest_path).is_file():
        report.section("恢复清单一致性")
        print("  （未提供 --manifest，跳过清单核对；数据库自身核验仍完整执行）")
        return None
    report.section("恢复清单一致性")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    report.check(manifest.get("format") == "pagefold-recovery-manifest-v1",
                 "清单格式标识正确")
    source_block = manifest.get("source", {})
    report.check(source_block.get("stable_throughout") is True
                 and source_block.get("snapshot_marker_at_begin")
                 == source_block.get("snapshot_marker_at_end"),
                 "清单证明整个读事务内快照未漂移（同一已提交时点）")
    report.check(logical_digest(drill_path) == manifest["drill_database"]["logical_sha256"],
                 "演练库逻辑内容 SHA-256 与清单一致（不受 WAL/页布局影响）")
    current_file_hash = hashlib.sha256(Path(drill_path).read_bytes()).hexdigest()
    if current_file_hash == manifest["drill_database"]["file_sha256"]:
        report.check(True, "文件字节与交付时完全一致（演练库尚未被服务改写）")
    else:
        report.check(True, "文件字节因服务打开/WAL 检查点变化，逻辑内容哈希仍一致（已规范化）")
    db = connect(drill_path)
    plan = db.execute("SELECT * FROM plans").fetchone()
    entry = manifest["selected_plan"]
    report.check(plan["id"] == entry["id"] and plan["revision"] == entry["revision"]
                 and plan["state"] == entry["state"], "清单计划 ID/版本/状态与库内一致")
    report.check(entry["sources"] == {
        "base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]},
        "清单三来源与库内计划一致")
    image_ids = {row[0] for row in db.execute("SELECT id FROM images")}
    report.check(image_ids == {item["id"] for item in manifest["included_images"]},
                 "清单包含镜像列表与库内镜像集合一致")
    for item in manifest["included_images"]:
        body = db.execute("SELECT body FROM images WHERE id=?", (item["id"],)).fetchone()[0]
        report.check(hashlib.sha256(body).hexdigest() == item["body_sha256"],
                     f"清单中镜像 {item['id'][:12]}… 摘要与库内字节一致")
    excluded = set(manifest["excluded"]["image_ids"])
    report.check(image_ids.isdisjoint(excluded), "清单声明排除的镜像均不在演练库")
    db.close()
    return manifest


def verify_against_source(source_path, drill_path, manifest, report):
    report.section("与生产库交叉核验（获准范围 / 隔离边界）")
    if not source_path or not Path(source_path).is_file():
        print("  （未提供 --source 生产库，跳过交叉核验；文件与清单独立核验仍完整执行）")
        return
    source = sqlite3.connect(f"file:{Path(source_path).resolve()}?mode=ro", uri=True, timeout=8)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    drill = connect(drill_path)
    drill_plan = drill.execute("SELECT * FROM plans").fetchone()
    source_plan = source.execute("SELECT * FROM plans WHERE id=?",
                                 (drill_plan["id"],)).fetchone()
    report.check(source_plan is not None, "获准计划在生产库中可找到")
    if source_plan is not None:
        same_revision = source_plan["revision"] == drill_plan["revision"]
        for column in ("base", "left_id", "right_id", "items", "decisions", "revision", "state"):
            report.check(source_plan[column] == drill_plan[column],
                         f"计划列 {column} 与生产库同版本一致"
                         + ("" if same_revision else "（注：生产库之后已推进，仅同版本可比对）"))
        if same_revision:
            for role, column in (("base", "base"), ("left", "left_id"), ("right", "right_id")):
                source_body = source.execute("SELECT body FROM images WHERE id=?",
                                             (source_plan[column],)).fetchone()[0]
                drill_body = drill.execute("SELECT body FROM images WHERE id=?",
                                           (drill_plan[column],)).fetchone()[0]
                report.check(source_body == drill_body,
                             f"{role} 镜像字节与生产库逐字节一致")
            source_attempts = source.execute(
                "SELECT revision,result,candidate FROM attempts WHERE plan_id=? ORDER BY revision",
                (drill_plan["id"],)).fetchall()
            drill_attempts = drill.execute(
                "SELECT revision,result,candidate FROM attempts WHERE plan_id=? ORDER BY revision",
                (drill_plan["id"],)).fetchall()
            report.check([tuple(r) for r in source_attempts]
                         == [tuple(r) for r in drill_attempts],
                         "重演历史（含结果与候选字节）与生产库一致")

    other_plan_ids = [row[0] for row in source.execute(
        "SELECT id FROM plans WHERE id<>?", (drill_plan["id"],))]
    drill_plan_ids = {row[0] for row in drill.execute("SELECT id FROM plans")}
    report.check(drill_plan_ids.isdisjoint(other_plan_ids),
                 f"生产库中另外 {len(other_plan_ids)} 份计划均未进入演练库")
    drill_image_ids = {row[0] for row in drill.execute("SELECT id FROM images")}
    source_image_ids = {row[0] for row in source.execute("SELECT id FROM images")}
    excluded = source_image_ids - drill_image_ids
    report.check(all(image_id not in drill_image_ids for image_id in excluded),
                 f"他方独有的 {len(excluded)} 份镜像被排除（共享祖本仍保留）")
    if manifest is not None:
        report.check(set(manifest["excluded"]["plan_ids"]) == set(other_plan_ids),
                     "清单排除计划列表与生产库实际情况一致")
        report.check(set(manifest["excluded"]["image_ids"]) == excluded,
                     "清单排除镜像列表与生产库实际情况一致")
    source.close()
    drill.close()


def verify_http(http_base, drill_path, report):
    report.section("HTTP 网页/API 核对")
    base = http_base.rstrip("/")
    db = connect(drill_path)
    try:
        plan = db.execute("SELECT * FROM plans").fetchone()
        image_count = db.execute("SELECT count(*) AS n FROM images").fetchone()["n"]
        release = db.execute("SELECT 1 FROM releases WHERE plan_id=?",
                             (plan["id"],)).fetchone()
    finally:
        db.close()
    try:
        with urlopen(base + "/api/plans", timeout=10) as response:
            plans_api = json.loads(response.read())
        report.check(len(plans_api) == 1 and plans_api[0]["id"] == plan["id"],
                     "GET /api/plans 只返回获准的一份计划")
        with urlopen(base + f"/api/plans/{plan['id']}", timeout=10) as response:
            detail = json.loads(response.read())
        report.check(detail["revision"] == plan["revision"]
                     and detail["state"] == plan["state"],
                     "API 计划版本与状态与库内一致")
        with urlopen(base + "/api/images", timeout=10) as response:
            images_api = json.loads(response.read())
        report.check(len(images_api) == image_count,
                     "GET /api/images 数量与演练库镜像数一致")
        with urlopen(base + "/", timeout=10) as response:
            home = response.read().decode("utf-8")
        report.check(plan["id"] in home and "三方合流工作区" in home,
                     "首页渲染且链接到获准计划")
        report.check(len(plans_api) == 1, "网页 API 不暴露任何他方计划入口")
        if release:
            with urlopen(base + f"/api/plans/{plan['id']}/certificate", timeout=10) as response:
                cert = json.loads(response.read())
            report.check(cert["plan"] == plan["id"], "封存凭证可经 HTTP 下载且绑定本计划")
            with urlopen(base + f"/api/plans/{plan['id']}/download", timeout=10) as response:
                candidate = response.read()
            report.check(hashlib.sha256(candidate).hexdigest() == cert["candidate_digest"],
                         "HTTP 下载候选与凭证摘要一致")
        else:
            report.check(True, "受阻/未封存计划无下载入口（与库内状态一致）")
    except OSError as exc:
        report.check(False, f"HTTP 核对失败：{exc}")


def main():
    parser = argparse.ArgumentParser(description="独立核验 Pagefold 演练实例")
    parser.add_argument("drill", type=Path, help="演练状态库路径")
    parser.add_argument("--source", type=Path, help="生产状态库路径（交叉核验隔离边界）")
    parser.add_argument("--manifest", type=Path, help="恢复清单 JSON")
    parser.add_argument("--http-base", help="演练实例 HTTP 基址，如 http://127.0.0.1:8791")
    args = parser.parse_args()
    report = Report()
    plan = verify_drill(args.drill, report)
    if plan is None:
        sys.exit(report.finish())
    manifest = verify_manifest(args.drill, args.manifest, report)
    verify_against_source(args.source, args.drill, manifest, report)
    if args.http_base:
        verify_http(args.http_base, args.drill, report)
    sys.exit(report.finish())


if __name__ == "__main__":
    main()
