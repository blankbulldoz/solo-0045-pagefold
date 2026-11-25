"""旧结构状态库的迁移演练：中断恢复、逐项验证与下载对照。

用历史提交中的旧代码通过真实 HTTP 工作流造出内嵌二进制的旧结构状态库
（含开放计划、失败重演、归档后的封存下载，以及同摘要跨来源、跨成果的
引用），然后：

1. 模拟中断（文件已写、状态未换）并确认旧结构在此期间完好可用；
2. 续执行迁移直到完成，确认幂等（重复运行不再改动）；
3. 运行 verify 逐项核对每个历史凭证与内容库字节一致；
4. 用新结构服务重新开放下载，与迁移前旧服务保存的对照字节逐一比对。

    python3 -m examples.migration_check
"""

import io
import json
import hashlib
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parent.parent
V1_COMMIT = "baec633d240a688561f84830ec52cff0f74cd4d2"  # 内嵌二进制旧结构的提交
DRIVER = Path(__file__).with_name("_v1_fixture_driver.py")

sys.path.insert(0, str(REPO))

from pagefold.store import schema_version  # noqa: E402


def expect(condition, message):
    if not condition:
        raise RuntimeError(message)


def run_cli(args, cwd, expect_code=0):
    result = subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != expect_code:
        raise RuntimeError(
            f"{args[-1] if args else args}: 退出码 {result.returncode} != {expect_code}\n"
            f"{result.stdout}\n{result.stderr}")
    return result


def extract_v1(dest):
    archive = subprocess.run(["git", "archive", V1_COMMIT], cwd=REPO, capture_output=True)
    if archive.returncode != 0:
        raise RuntimeError(f"无法取出旧结构代码（需要提交 {V1_COMMIT[:12]}）：{archive.stderr.decode()}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest)


def query(state, sql, args=()):
    db = sqlite3.connect(state)
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200):
        headers = {}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=headers)
        try:
            with urlopen(request, timeout=10) as result:
                body, actual = result.read(), result.status
        except HTTPError as error:
            with error:
                body, actual = error.read(), error.code
        expect(actual == status, f"{route}: {actual} != {status}: {body[:300]}")
        return json.loads(body) if body[:1] in (b"{", b"[") else body


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-migration-") as root:
        root = Path(root)
        state, golden, v1src = root / "state.sqlite", root / "golden", root / "v1src"
        extract_v1(v1src)
        summary = json.loads(run_cli(
            ["-I", str(DRIVER), str(v1src), str(state), str(golden)], cwd=root).stdout)
        size_before = state.stat().st_size

        # 造出的状态库确为旧结构：三处二进制内嵌，无 meta 表。
        expect(schema_version(sqlite3.connect(state)) == "1", "演练状态库应为旧结构")
        expect(len(query(state, "SELECT id FROM images")) == 5, "镜像行数不符")
        expect(len(query(state, "SELECT * FROM attempts")) == 3, "重演行数不符")
        expect(len(query(state, "SELECT * FROM releases")) == 2, "封存行数不符")
        for tag in ("planA", "planD"):
            row = query(state, "SELECT candidate FROM releases WHERE plan_id=?",
                        (summary[tag],))[0]
            expect(row[0] == (golden / f"{tag}.download").read_bytes(),
                   f"{tag} 旧库内嵌字节与旧服务下载不符")
        print("fixture ✓ 旧结构状态库：开放计划、失败重演、归档封存、重复内容各就各位")

        # 未迁移的状态库不能通过新结构校验。
        run_cli(["-m", "pagefold.migrate", "verify", str(state)], cwd=REPO, expect_code=1)
        print("verify-before ✓ 旧结构尚未迁移，校验按预期拒绝")

        # 模拟中断：第 5 份文件写入后、所在批次提交前（文件已写、状态未换）。
        run_cli(["-m", "pagefold.migrate", "run", str(state), "--batch", "4",
                 "--stop-after", "5", "--no-vacuum"], cwd=REPO, expect_code=3)
        blobs = Path(str(state) + ".blobs")
        expect(schema_version(sqlite3.connect(state)) == "1", "中断后旧结构应保持可用")
        expect(len(query(state, "SELECT id FROM images_migrating")) == 4, "中断点已提交批次不符")
        expect(len(query(state, "SELECT * FROM attempts_migrating")) == 0, "未提交批次不应留行")
        expect(len(list(blobs.iterdir())) == 5, "中断点内容库文件数不符")
        row = query(state, "SELECT candidate FROM releases WHERE plan_id=?", (summary["planA"],))[0]
        expect(row[0] == (golden / "planA.download").read_bytes(), "中断后旧表下载字节改变")
        print("interrupt ✓ 第 5 份文件已写而所属批次未提交，旧结构毫发无损")

        # 续执行：复用已写文件，完成复制、预校验、原子切换和页面回收。
        run_cli(["-m", "pagefold.migrate", "run", str(state)], cwd=REPO)
        expect(schema_version(sqlite3.connect(state)) == "2", "迁移后应为新结构")
        expect(not query(state, "SELECT name FROM sqlite_master WHERE name LIKE '%_migrating'"),
               "影子表应已转正")
        files = sorted(path.name for path in blobs.iterdir())
        expect(len(files) == 7, f"内容库应为 7 份去重文件，实为 {len(files)}")
        again = run_cli(["-m", "pagefold.migrate", "run", str(state)], cwd=REPO)
        expect("无需迁移" in again.stdout, "重复迁移应幂等")
        print(f"resume ✓ 续执行完成并幂等；状态库 {size_before:,} → {state.stat().st_size:,} 字节，"
              f"内容库 {sum(path.stat().st_size for path in blobs.iterdir()):,} 字节")

        # 逐项验证：每个历史凭证与内容库字节一致。
        report = json.loads(run_cli(["-m", "pagefold.migrate", "verify", str(state), "--json"],
                                    cwd=REPO).stdout)
        expect(report["problems"] == [], f"校验发现问题：{report['problems']}")
        expect(report["warnings"] == [], f"校验出现警告：{report['warnings']}")
        expect((report["images"], report["attempts"], report["releases"]) == (5, 3, 2),
               "迁移后行数不符")
        expect((report["references"], report["blobs"]) == (9, 7), "内容库去重计数不符")
        print("verify ✓ 5 镜像、3 重演、2 凭证逐一字节一致；9 次引用去重为 7 份文件")

        # 下载对照：新结构服务与迁移前旧服务字节一致。
        from pagefold.server import make_server
        from pagefold.store import Store
        with make_server(Store(state), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                for tag in ("planA", "planD"):
                    route = f"/api/plans/{summary[tag]}"
                    expect(client.call(route + "/download") == (golden / f"{tag}.download").read_bytes(),
                           f"{tag} 迁移后下载字节改变")
                    expect(client.call(route + "/certificate") ==
                           json.loads((golden / f"{tag}.certificate.json").read_text("utf-8")),
                           f"{tag} 迁移后凭证改变")
                expect(client.call(f"/api/plans/{summary['planB']}/attempts") ==
                       json.loads((golden / "planB.attempts.json").read_text("utf-8")),
                       "失败重演历史改变")
                expect(client.call(f"/api/plans/{summary['planC']}") ==
                       json.loads((golden / "planC.plan.json").read_text("utf-8")), "开放计划状态改变")
                expect(client.call("/api/images") == json.loads((golden / "images.json").read_text("utf-8")),
                       "镜像列表改变")
                quartz = summary["quartz"]
                expect(client.call(f"/api/images/{quartz}") ==
                       json.loads((golden / "quartz.catalog.json").read_text("utf-8")), "镜像目录改变")
                expect(client.call(f"/api/images/{quartz}/rows?table=node") ==
                       json.loads((golden / "quartz.rows.node.json").read_text("utf-8")), "镜像行数据改变")
                plan_a = client.call(f"/api/plans/{summary['planA']}")
                expect(plan_a["state"] == "archived", "归档状态丢失")
                item = next(i["id"] for i in plan_a["items"] if not i["automatic"])
                closed = client.call(f"/api/plans/{summary['planA']}/decisions",
                                     {"item": item, "choice": "base", "revision": 1}, 409)
                expect(closed["code"] == "closed_plan", "归档计划仍可写入")
                print("download ✓ 归档与封存计划下载、凭证、历史、目录、行页与旧服务逐项一致")

                # 迁移后的状态库完整可写：新计划走通裁决、重演、封存、下载。
                fresh = client.call("/api/plans", {"base": quartz, "left": summary["left"],
                                                   "right": summary["right"]}, 201)
                route = f"/api/plans/{fresh['id']}"
                conflict = next(i["id"] for i in fresh["items"] if not i["automatic"])
                client.call(route + "/decisions", {"item": conflict, "choice": "left", "revision": 0})
                expect(client.call(route + "/rehearse", {"revision": 1})["status"] == "passed",
                       "迁移后新计划重演失败")
                certificate = client.call(route + "/seal", {"revision": 1})
                expect(hashlib.sha256(client.call(route + "/download")).hexdigest()
                       == certificate["candidate_digest"], "迁移后新封存下载与凭证不符")
                print("write ✓ 迁移后的状态库完整可写：新计划裁决、重演、封存、下载走通")
            finally:
                server.shutdown()
                thread.join()
        print("migration-check ✓ 迁移、中断恢复、验证与下载对照全部通过")


if __name__ == "__main__":
    main()
