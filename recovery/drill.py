"""按计划隔离恢复的端到端恢复演练。

场景
----
生产状态库中有两个团队的计划，共享同一份祖本 quartz：
- 计划 A（青蓝×赤褐）：完成裁决、重演、封存并归档——“已归档但可下载”。
- 计划 B（stress-left×stress-right）：重演受阻——“只是受阻”，其独有镜像不得外泄。

演练证明
--------
1. 生产实例全程在 8745 端口可用，继续接受封存/归档等写入。
2. 取证恢复期间生产库有并发写入，演练库整体来自同一个已提交时点（快照隔离）。
3. 计划 A 的演练实例只含获准闭包；计划 B 独有的镜像、计划、候选全部被排除。
4. 中断后留下的 .part 不会被当成可交接实例；重跑产出的库通过全部核验。
5. 受阻计划 B 也能按计划恢复（只有事实，没有可下载候选）。
6. 网页（真实 HTTP）与 SQLite（独立连接）两种方式都能核对结果。

用法：python3 -m recovery.drill [--root 目录] [--keep]
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.attempts import rehearse
from pagefold.archive import archive
from pagefold.decisions import resolve
from pagefold.demo import image
from pagefold.images import register
from pagefold.plans import create_plan
from pagefold.releases import seal
from pagefold.server import make_server
from pagefold.store import Store

from .recover import recover
from .verify import logical_digest, main as verify_main

ROOT = Path(__file__).resolve().parent.parent


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, route, data=None, status=200):
        headers = {}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base + route, data=data, headers=headers)
        try:
            with urlopen(request, timeout=10) as result:
                body, code, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, code, kind = error.read(), error.code, error.headers.get_content_type()
        assert code == status, f"{route}: {code} != {status}: {body[:300]}"
        return json.loads(body) if kind == "application/json" else body

    def upload(self, flavor):
        request = Request(self.base + "/api/images", data=image(flavor),
                          headers={"X-Image-Name": flavor,
                                   "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            return json.loads(result.read())


def step(title):
    print(f"\n\033[1m■ {title}\033[0m")


def seed_production(store):
    """在生产库中建立两个共享祖本的团队计划。"""
    quartz = register(store, image("quartz"), "石英祖本（共享）")
    cerulean = register(store, image("cerulean"), "青蓝分支（团队A）")
    umber = register(store, image("umber"), "赤褐分支（团队A）")
    stress_left = register(store, image("stress-left"), "加压左支（团队B 独有）")
    stress_right = register(store, image("stress-right"), "加压右支（团队B 独有）")

    plan_a = create_plan(store, {"base": quartz["id"], "left": cerulean["id"],
                                 "right": umber["id"]})
    conflict = next(item for item in plan_a["items"] if not item["automatic"])
    first = resolve(store, plan_a["id"], 0, conflict["id"], "right")  # rev 1
    passed = rehearse(store, plan_a["id"], first["revision"])
    assert passed["status"] == "passed"
    resolve(store, plan_a["id"], 1, conflict["id"], "base")             # rev 2
    current = rehearse(store, plan_a["id"], 2)
    certificate = seal(store, plan_a["id"], 2)
    archive(store, plan_a["id"], 2)

    plan_b = create_plan(store, {"base": quartz["id"], "left": stress_left["id"],
                                 "right": stress_right["id"]})
    blocked = rehearse(store, plan_b["id"], 0)
    assert blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint"
    return {
        "quartz": quartz["id"], "cerulean": cerulean["id"], "umber": umber["id"],
        "stress_left": stress_left["id"], "stress_right": stress_right["id"],
        "plan_a": plan_a["id"], "plan_b": plan_b["id"],
        "certificate_a": certificate, "candidate_digest_a": current["candidate_digest"],
    }


class ConcurrentWrites(Thread):
    """模拟取证期间生产库的并发写入：持续登记带唯一字节的新镜像。"""

    def __init__(self, client):
        super().__init__(daemon=True)
        self.client = client
        self.stop = Event()
        self.count = 0

    def run(self):
        while not self.stop.is_set():
            payload = sqlite3.connect(":memory:")
            try:
                payload.executescript("CREATE TABLE t(k TEXT PRIMARY KEY) WITHOUT ROWID;")
                payload.execute("INSERT INTO t VALUES(?)", (f"forensic-{time.time_ns()}-{self.count}",))
                blob = payload.serialize()
            finally:
                payload.close()
            request = Request(self.client.base + "/api/images", data=blob, headers={
                "X-Image-Name": f"forensic-write-{self.count}",
                "Content-Type": "application/octet-stream"})
            try:
                with urlopen(request, timeout=10) as result:
                    json.loads(result.read())
                self.count += 1
            except (HTTPError, OSError):
                pass
            time.sleep(0.02)


def run_verifier(argv):
    print("$ python3 -m recovery.verify " + " ".join(argv))
    code = 0
    original_argv = sys.argv
    try:
        sys.argv = ["recovery.verify", *argv]
        verify_main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    finally:
        sys.argv = original_argv
    assert code == 0, "独立核验未通过"


def sqlite_probe(path, sql):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Pagefold 按计划隔离恢复演练")
    parser.add_argument("--root", type=Path, help="演练产物目录（默认 ./drill-artifacts）")
    args = parser.parse_args()

    root = args.root or (ROOT / "drill-artifacts")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    production_db = root / "production" / "pagefold-state.sqlite"
    production_db.parent.mkdir()
    drill_a_db = root / "plan-a" / "drill-a.sqlite"
    drill_b_db = root / "plan-b" / "drill-b.sqlite"
    drill_a_db.parent.mkdir()
    drill_b_db.parent.mkdir()

    step("1. 建立生产状态库：两个共享祖本的团队计划")
    ids = seed_production(Store(production_db))
    print(f"生产库：{production_db}")
    print(f"  计划 A（已归档、已封存）：{ids['plan_a']}")
    print(f"  计划 B（重演受阻、开放）  ：{ids['plan_b']}")
    print(f"  共享祖本 quartz         ：{ids['quartz']}")
    print(f"  团队B 独有镜像          ：{ids['stress_left'][:12]}… / {ids['stress_right'][:12]}…")

    step("2. 在另一端口启动生产实例（8745），全程保持可用")
    production = make_server(Store(production_db), 8745)
    thread = Thread(target=production.serve_forever, daemon=True)
    thread.start()
    prod_client = Client("http://127.0.0.1:8745")
    assert len(prod_client.call("/api/plans")) == 2
    print("生产实例已启动：http://127.0.0.1:8745 （2 份计划、5 份镜像）")

    try:
        step("3. 取证期间并发写入，恢复计划 A —— 演练库必须来自同一已提交时点")
        before_images = len(prod_client.call("/api/images"))
        writer = ConcurrentWrites(prod_client)
        writer.start()
        time.sleep(0.1)
        manifest_a = recover(production_db, drill_a_db, ids["plan_a"])
        time.sleep(0.1)
        writer.stop.set()
        writer.join(timeout=5)
        after_images = len(prod_client.call("/api/images"))
        begin_marker = manifest_a["source"]["snapshot_marker_at_begin"]
        end_marker = manifest_a["source"]["snapshot_marker_at_end"]
        print(f"恢复进行中并发登记镜像 {writer.count} 份；"
              f"生产 images {before_images} → {after_images}")
        print(f"读事务开始 images={begin_marker['image_count']}，"
              f"事务结束仍为 {end_marker['image_count']}（事务视角冻结）")
        assert manifest_a["source"]["stable_throughout"] is True, "读事务内快照发生漂移"
        assert begin_marker["image_count"] < after_images, \
            "事务进行中应有并发提交，才能证明快照隔离而非串行巧合"
        drill_images = sqlite_probe(drill_a_db, "SELECT count(*) FROM images")[0][0]
        assert drill_images == 3, f"计划 A 闭包应只有 3 份镜像，实际 {drill_images}"
        assert after_images > before_images, "并发写入未生效，演示前提不成立"
        # 事务开始之后提交的镜像 rowid 必然大于 begin 标记，演练库中不得出现任何一份。
        probe = sqlite3.connect(drill_a_db)
        try:
            max_rowid = probe.execute("SELECT coalesce(max(rowid),0) FROM images").fetchone()[0]
        finally:
            probe.close()
        assert max_rowid <= begin_marker["max_image_rowid"], "快照之后的新行混入演练库"
        print(f"演练库 images={drill_images}（quartz/cerulean/umber）；"
              f"事务进行中提交的 {after_images - begin_marker['image_count']} 份新镜像零混入 ✓")

        step("4. 生产实例继续处理封存/归档类工作且保持可用")
        prod_plans = prod_client.call("/api/plans")
        assert {row["id"] for row in prod_plans} == {ids["plan_a"], ids["plan_b"]}
        detail_b = prod_client.call(f"/api/plans/{ids['plan_b']}")
        assert detail_b["state"] == "open"
        # 对受阻计划再做一次幂等重演，证明生产写入链路照常服务。
        again = prod_client.call(f"/api/plans/{ids['plan_b']}/rehearse", {"revision": 0})
        assert again["status"] == "blocked"
        home = prod_client.call("/")
        assert ids["plan_a"].encode() in home and ids["plan_b"].encode() in home
        print("生产库两份计划均可经网页访问，受阻计划继续返回诊断，生产服务未中断 ✓")

        step("5. 中断演练：在装配中途杀死恢复进程，半成品不得成为可交接实例")
        # 第一次运行带 --crash-point images：建表后、写计划前硬退出。
        proc = subprocess.run(
            [sys.executable, "-m", "recovery.recover", "--source", str(production_db),
             "--output", str(drill_a_db), "--plan", ids["plan_a"],
             "--crash-point", "images"],
            cwd=ROOT, capture_output=True, text=True)
        assert proc.returncode == 86, proc.stderr
        part = drill_a_db.with_name(drill_a_db.name + ".part")
        assert part.exists(), "中断后应留下 .part 暂存文件"
        probe = sqlite3.connect(part)
        try:
            tables = {row[0] for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            plan_rows = probe.execute("SELECT count(*) FROM plans").fetchone()[0]
        finally:
            probe.close()
        assert "plans" in tables and plan_rows == 0, "暂存件里不应有任何计划数据"
        print(f"中断留下暂存件 {part.name}：表结构存在但 plans=0，不可交接 ✓")
        # 核验器必须拒绝把暂存件当成交付库。
        reject = subprocess.run(
            [sys.executable, "-m", "recovery.verify", str(part)],
            cwd=ROOT, capture_output=True, text=True)
        assert reject.returncode != 0 and "恰好一份计划" in reject.stdout + reject.stderr
        print("核验器拒绝暂存件（非零退出，提示计划数不符）✓")

        step("6. 原命令重跑：清理暂存件、原子产出完整演练库，全部核验通过")
        manifest_rerun = recover(production_db, drill_a_db, ids["plan_a"])
        assert not part.exists(), "重跑后暂存件应被清除"
        assert drill_a_db.exists()
        print(f"重跑完成：{drill_a_db}（清单 {drill_a_db.with_suffix('.manifest.json').name}）")
        run_verifier([str(drill_a_db), "--source", str(production_db),
                      "--manifest", str(drill_a_db.with_suffix(".manifest.json"))])

        step("7. SQLite 独立核对：隔离边界、封存字节与候选内容")
        rows = sqlite_probe(drill_a_db, "SELECT id,state,revision FROM plans")
        assert rows == [(ids["plan_a"], "archived", 2)], rows
        probe = sqlite3.connect(drill_a_db)
        try:
            leaked = [row[0] for row in probe.execute(
                "SELECT id FROM images WHERE id IN (?,?)",
                (ids["stress_left"], ids["stress_right"]))]
            assert not leaked, f"团队B 独有镜像泄漏：{leaked}"
            candidate = probe.execute("SELECT candidate FROM releases").fetchone()[0]
            assert hashlib.sha256(candidate).hexdigest() == ids["candidate_digest_a"]
            mem = sqlite3.connect(":memory:")
            try:
                mem.deserialize(candidate)
                note = mem.execute(
                    "SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()[0]
                fk = mem.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                mem.close()
            assert note == "祖本节点" and fk == []
            # 必要历史：两次重演（rev1 通过、rev2 通过）都在。
            revisions = [row[0] for row in probe.execute(
                "SELECT revision FROM attempts ORDER BY revision")]
            assert revisions == [1, 2], revisions
        finally:
            probe.close()
        print("演练库仅含计划 A（archived rev2），团队B 镜像零泄漏；")
        print("封存候选可独立打开，裁决结果 note='祖本节点'、外键无违规；重演历史 rev1/rev2 完整 ✓")

        step("8. 演练实例在另一端口（8791）提供网页，真实 HTTP 核对")
        drill_server = make_server(Store(drill_a_db), 8791)
        drill_thread = Thread(target=drill_server.serve_forever, daemon=True)
        drill_thread.start()
        try:
            run_verifier([str(drill_a_db), "--source", str(production_db),
                          "--manifest", str(drill_a_db.with_suffix(".manifest.json")),
                          "--http-base", "http://127.0.0.1:8791"])
            api_plans = Client("http://127.0.0.1:8791").call("/api/plans")
            assert len(api_plans) == 1 and api_plans[0]["id"] == ids["plan_a"]
            missing = Client("http://127.0.0.1:8791").call(
                f"/api/plans/{ids['plan_b']}", status=404)
            assert missing["code"] == "unknown_plan"
        finally:
            drill_server.shutdown()
            drill_thread.join(timeout=5)
        print("演练网页只见计划 A；直接访问计划 B 返回 404 unknown_plan ✓")

        step("9. 受阻计划 B 同样可按计划隔离恢复（只带事实，不带可下载候选）")
        manifest_b = recover(production_db, drill_b_db, ids["plan_b"])
        probe = sqlite3.connect(drill_b_db)
        try:
            state = probe.execute("SELECT state,revision FROM plans").fetchone()
            assert tuple(state) == ("open", 0)
            blocked_attempt = json.loads(probe.execute(
                "SELECT result FROM attempts").fetchone()[0])
            assert blocked_attempt["status"] == "blocked"
            assert probe.execute("SELECT candidate FROM attempts").fetchone()[0] is None
            assert probe.execute("SELECT count(*) FROM releases").fetchone()[0] == 0
            image_ids = {row[0] for row in probe.execute("SELECT id FROM images")}
        finally:
            probe.close()
        assert image_ids == {ids["quartz"], ids["stress_left"], ids["stress_right"]}
        assert ids["cerulean"] not in image_ids and ids["umber"] not in image_ids
        print("计划 B 演练库含 quartz + 两份 stress 镜像；团队A 的 cerulean/umber 被排除；")
        print("受阻重演记录保留诊断且 candidate 为 NULL，无 release 可下载 ✓")

        drill_b_server = make_server(Store(drill_b_db), 8792)
        drill_b_thread = Thread(target=drill_b_server.serve_forever, daemon=True)
        drill_b_thread.start()
        try:
            run_verifier([str(drill_b_db), "--source", str(production_db),
                          "--manifest", str(drill_b_db.with_suffix(".manifest.json")),
                          "--http-base", "http://127.0.0.1:8792"])
            download = Client("http://127.0.0.1:8792").call(
                f"/api/plans/{ids['plan_b']}/download", status=404)
            assert download["code"] == "unpublished"
        finally:
            drill_b_server.shutdown()
            drill_b_thread.join(timeout=5)
        print("受阻计划的演练网页对下载请求返回 404 unpublished ✓")

        step("10. 恢复后生产库继续推进：新时点不影响已冻结的演练实例")
        # 生产侧再登记一份新镜像，并确认演练库逻辑内容保持冻结。
        digest_before = logical_digest(drill_a_db)
        fresh = sqlite3.connect(":memory:")
        try:
            fresh.executescript("CREATE TABLE t(k TEXT PRIMARY KEY) WITHOUT ROWID;")
            fresh.execute("INSERT INTO t VALUES(?)", ("post-recovery",))
            blob = fresh.serialize()
        finally:
            fresh.close()
        request = Request("http://127.0.0.1:8745/api/images", data=blob, headers={
            "X-Image-Name": "post-recovery-image", "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            json.loads(result.read())
        assert logical_digest(drill_a_db) == digest_before, "演练库逻辑内容被生产侧写入污染"
        final_prod_images = len(prod_client.call("/api/images"))
        print(f"生产 images 继续增长至 {final_prod_images}，演练库逻辑 SHA-256 不变 ✓")
    finally:
        production.shutdown()
        thread.join(timeout=5)

    step("演练产物与复核命令")
    for path in sorted(root.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size} B)")
    print("\n独立复核（SQLite 方式）：")
    print(f"  python3 -m recovery.verify {drill_a_db.relative_to(ROOT)} "
          f"--source {production_db.relative_to(ROOT)} "
          f"--manifest {drill_a_db.with_suffix('.manifest.json').relative_to(ROOT)}")
    print("\n启动演练实例（网页方式）：")
    print(f"  python3 -m pagefold --state {drill_a_db.relative_to(ROOT)} --port 8791")
    print("\n全部演练断言通过。")


if __name__ == "__main__":
    main()
