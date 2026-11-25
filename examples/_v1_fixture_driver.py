"""在旧结构（v1）代码下通过真实 HTTP 工作流生成迁移演练用的状态库。

由 examples/migration_check.py 以子进程方式调用，sys.path 指向从历史
提交取出的旧代码，因此本文件自身不导入任何新结构符号：

    python3 -I examples/_v1_fixture_driver.py <旧代码目录> <状态库路径> <对照目录>

对照目录保存旧服务下载的镜像与凭证字节，供迁移后逐项比对。
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

V1SRC, STATE, GOLDEN = map(Path, sys.argv[1:4])
sys.path.insert(0, str(V1SRC))

import pagefold  # noqa: E402

origin = Path(list(pagefold.__path__)[0]).resolve()
if not str(origin).startswith(str(V1SRC.resolve())):
    raise SystemExit(f"导入了错误位置的 pagefold：{origin}")

from pagefold.demo import image  # noqa: E402
from pagefold.server import make_server  # noqa: E402
from pagefold.store import Store  # noqa: E402


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
        if actual != status:
            raise RuntimeError(f"{route}: {actual} != {status}: {body[:300]}")
        return json.loads(body) if body[:1] in (b"{", b"[") else body

    def upload(self, name, label):
        from urllib.parse import quote
        request = Request(self.origin + "/api/images", image(name),
                          {"X-Image-Name": quote(label), "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            return json.loads(result.read())


def main():
    GOLDEN.mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with make_server(Store(STATE), 0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            client = Client(server.server_port)
            quartz = client.upload("quartz", "石英祖本")
            # 同一内容重复登记：旧结构只保留一行，新结构只保留一份文件。
            again = client.upload("quartz", "重复登记")
            if again["id"] != quartz["id"]:
                raise RuntimeError("重复镜像未复用")
            left = client.upload("cerulean", "青蓝分支")
            right = client.upload("umber", "赤褐分支")
            stress_left = client.upload("stress-left", "压力左支")
            stress_right = client.upload("stress-right", "压力右支")

            def create_plan(base, l, r):
                return client.call("/api/plans", {"base": base, "left": l, "right": r}, 201)

            def conflict_id(plan):
                return next(item["id"] for item in client.call(f"/api/plans/{plan['id']}")["items"]
                            if not item["automatic"])

            # 计划 A：裁决 → 重演通过 → 封存 → 归档（归档后的封存下载）。
            plan_a = create_plan(quartz["id"], left["id"], right["id"])
            route_a = f"/api/plans/{plan_a['id']}"
            client.call(route_a + "/decisions",
                        {"item": conflict_id(plan_a), "choice": "right", "revision": 0}, 200)
            if client.call(route_a + "/rehearse", {"revision": 1})["status"] != "passed":
                raise RuntimeError("计划 A 重演未通过")
            client.call(route_a + "/seal", {"revision": 1})
            client.call(route_a + "/archive", {"revision": 1})

            # 计划 B：失败重演（候选为空），保持开放。
            plan_b = create_plan(quartz["id"], stress_left["id"], stress_right["id"])
            route_b = f"/api/plans/{plan_b['id']}"
            if client.call(route_b + "/rehearse", {"revision": 0})["status"] != "blocked":
                raise RuntimeError("计划 B 应当重演失败")

            # 计划 C：有裁决但未重演，保持开放。
            plan_c = create_plan(quartz["id"], left["id"], right["id"])
            route_c = f"/api/plans/{plan_c['id']}"
            client.call(route_c + "/decisions",
                        {"item": conflict_id(plan_c), "choice": "base", "revision": 0}, 200)

            # 计划 D：祖本同时充当左支（同摘要跨来源角色），封存但不归档。
            plan_d = create_plan(quartz["id"], quartz["id"], right["id"])
            route_d = f"/api/plans/{plan_d['id']}"
            if client.call(route_d + "/rehearse", {"revision": 0})["status"] != "passed":
                raise RuntimeError("计划 D 重演未通过")
            client.call(route_d + "/seal", {"revision": 0})

            for tag, route in (("planA", route_a), ("planD", route_d)):
                download = client.call(route + "/download")
                certificate = client.call(route + "/certificate")
                if hashlib.sha256(download).hexdigest() != certificate["candidate_digest"]:
                    raise RuntimeError(f"{tag} 旧服务下载与凭证不符")
                (GOLDEN / f"{tag}.download").write_bytes(download)
                (GOLDEN / f"{tag}.certificate.json").write_text(json.dumps(certificate), "utf-8")
            (GOLDEN / "planB.attempts.json").write_text(
                json.dumps(client.call(route_b + "/attempts")), "utf-8")
            (GOLDEN / "planC.plan.json").write_text(json.dumps(client.call(route_c)), "utf-8")
            (GOLDEN / "images.json").write_text(json.dumps(client.call("/api/images")), "utf-8")
            (GOLDEN / "quartz.catalog.json").write_text(
                json.dumps(client.call(f"/api/images/{quartz['id']}")), "utf-8")
            (GOLDEN / "quartz.rows.node.json").write_text(
                json.dumps(client.call(f"/api/images/{quartz['id']}/rows?table=node")), "utf-8")
            summary = {"quartz": quartz["id"], "left": left["id"], "right": right["id"],
                       "stress_left": stress_left["id"], "stress_right": stress_right["id"],
                       "planA": plan_a["id"], "planB": plan_b["id"],
                       "planC": plan_c["id"], "planD": plan_d["id"]}
        finally:
            server.shutdown()
            thread.join()
    db = sqlite3.connect(STATE)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
