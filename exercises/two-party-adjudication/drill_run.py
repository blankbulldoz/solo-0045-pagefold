#!/usr/bin/env python3
"""两人复合键裁决演习 —— 可实跑的操作版序。

通过真实 HTTP 入口复现整场演习：
  1. 登记 quartz(祖本)/cerulean(左支)/umber(右支) 三份镜像并建立合流计划；
  2. 第一组（守左支 cerulean）持 revision=0 先提交，选择右支被接受，版本推进到 1；
  3. 第二组（守右支 umber）工作页停留在 revision=0，提交左支被 stale_revision 拒绝；
  4. 第二组刷新到 revision=1 后重新提交，改选左支被接受，版本推进到 2；
  5. 重演 revision=2、封存、下载，核对最终行集与凭证摘要。

用法：
  python3 drill_run.py                 # 启动临时服务，跑完整流程并把结果写入 evidence/
  python3 drill_run.py --origin URL    # 对已在运行的 Pagefold 服务执行（不自行启停服务）
  python3 drill_run.py --out DIR       # 指定证据输出目录（默认 脚本旁的 evidence/）

仅依赖标准库与仓库内的 pagefold 包。脚本不修改三份输入镜像。
"""
import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pagefold.demo import image  # noqa: E402

# 内容型指纹由三方镜像内容决定，跨运行稳定；plan_id 每次新建随机。
EXPECTED = {
    "image_ids": {
        "base": "90438d5a76cd0744920d78acb009df1d19aa23baad0a7ed6775927b53e7a1a25",
        "left": "ab1bceb83ee5599db0a06e4429977951f26378db082cbffa052f8215a14d6f75",
        "right": "d6a547d0982d6150b30a8a78706c3db162c3d339c41ee918a718e46dce39a009",
    },
    "conflict_id": "3d9fecf2f8e0e841605f0984d234e878b149ba4a0758ce68606e4c9543b0c3bf",
    "automatic_bench_id": "836ae6cbe88b89d5ab4061b352a8bba0f34bdb4553b1417311b78d6f50eb4065",
    "automatic_node_id": "1ee048a2b874422c6d36d4111f8f45b9673030fa0388f262f92b7c2d85444276",
    "candidate_digest": "8d09772f0076e47abba9eee777569d717b3758e8ca92881f8758b66d40300da0",
    "decision_digest": "01672756d23e15b5bc68bd117637ce7e6cf648254a2f2427279e95ac3807a95a",
    "plan_digest": "a9d19d0259fa61f4fac8ab9119ecce992be8764d5e66df3b27808e00f4722f32",
    "schema_digest": "2850b20c7d73163e0e8f153ae064af159f3389cc6d3ed1a5c909218ba29fb6ac",
}


class Check:
    def __init__(self):
        self.passed, self.failed = [], []

    def that(self, name, condition, detail=""):
        (self.passed if condition else self.failed).append({"check": name, "detail": detail})
        mark = "✓" if condition else "✗"
        print(f"  {mark} {name}" + (f" —— {detail}" if detail and not condition else ""))

    def report(self):
        return {"passed": len(self.passed), "failed": len(self.failed),
                "failures": self.failed}


def call(origin, route, data=None, status=200):
    headers = {}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = Request(origin + route, data=data, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            body, code, kind = response.read(), response.status, response.headers.get_content_type()
    except HTTPError as error:
        with error:
            body, code, kind = error.read(), error.code, error.headers.get_content_type()
    if code != status:
        raise AssertionError(f"{route} 返回 {code}，预期 {status}：{body[:300]!r}")
    return json.loads(body) if kind == "application/json" else body


def upload(origin, name):
    request = Request(origin + "/api/images", data=image(name), headers={
        "X-Image-Name": name, "Content-Type": "application/octet-stream"})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def present(value):
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    return value


def dump_tables(candidate_bytes):
    import sqlite3
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate_bytes)
        result = {}
        for table in ("bench", "node", "binding"):
            result[table] = [[present(cell) for cell in row]
                             for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
        return result
    finally:
        db.close()


def run(origin, check):
    log = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "origin": origin, "timeline": []}

    def step(title, payload):
        print(f"\n【{title}】")
        log["timeline"].append({"step": title, **payload})

    # 0. 登记三份镜像
    images = {role: upload(origin, name) for role, name in
              (("base", "quartz"), ("left", "cerulean"), ("right", "umber"))}
    check.that("三份镜像按内容 SHA-256 登记，且与固定指纹一致",
               {role: meta["id"] for role, meta in images.items()} == EXPECTED["image_ids"],
               json.dumps({role: meta["id"] for role, meta in images.items()}))
    log["image_ids"] = {role: meta["id"] for role, meta in images.items()}

    # 1. 建立计划
    sources = {role: meta["id"] for role, meta in images.items()}
    plan = call(origin, "/api/plans", sources, 201)
    route = f"/api/plans/{plan['id']}"
    log["plan_id"] = plan["id"]
    check.that("计划初始 revision=0，含 3 个差异条目，1 项待裁决",
               plan["revision"] == 0 and len(plan["items"]) == 3 and plan["unresolved"] == 1)
    conflict = next(item for item in plan["items"] if item["automatic"] is None)
    check.that("唯一冲突是复合键 node('north',1)，条目指纹固定",
               conflict["id"] == EXPECTED["conflict_id"]
               and conflict["table"] == "node" and conflict["key"] == [["t", "north"], ["i", "1"]],
               conflict["id"])
    autos = {(item["id"], item["automatic"]) for item in plan["items"] if item["automatic"]}
    check.that("另有两条自动采纳行（bench 右支、node 左支），无需人工裁决",
               autos == {(EXPECTED["automatic_bench_id"], "right"),
                         (EXPECTED["automatic_node_id"], "left")},
               json.dumps(sorted(autos), ensure_ascii=False))

    # 2. 未决不能重演
    blocked = call(origin, route + "/rehearse", {"revision": 0}, 409)
    check.that("存在未裁决项时重演被 409 unresolved 拦截", blocked["code"] == "unresolved", blocked["code"])
    step("建立计划", {"revision": 0, "unresolved": 1, "conflict_id": conflict["id"],
                     "automatic": [{"id": i, "choice": c} for i, c in sorted(autos)]})

    # 3. 第一组先提交（他们守左支，但本轮先就右支口径试投，成功落库）
    first = call(origin, route + "/decisions",
                 {"revision": 0, "item": conflict["id"], "choice": "right"})
    check.that("第一组持 revision=0 的提交被接受，版本推进到 1，冲突选择 right",
               first["revision"] == 1 and first["choice"] == "right")
    step("第一组提交（工作页 revision=0 → 接受）",
         {"submitted_revision": 0, "choice": "right", "accepted": True, "new_revision": 1})

    # 4. 第二组用过期工作页提交，被拒绝
    stale = call(origin, route + "/decisions",
                 {"revision": 0, "item": conflict["id"], "choice": "left"}, 409)
    check.that("第二组持过期 revision=0 的提交被 409 stale_revision 拒绝，且不产生新版本",
               stale["code"] == "stale_revision" and call(origin, route)["revision"] == 1,
               stale["code"])
    step("第二组提交（工作页停留在 revision=0 → 拒绝）",
         {"submitted_revision": 0, "choice": "left", "accepted": False,
          "error": stale["code"], "revision_remains": 1})

    # 5. 第二组刷新后重新提交
    refreshed = call(origin, route)
    current_choice = next(item for item in refreshed["items"]
                          if item["id"] == conflict["id"])["choice"]
    check.that("第二组刷新后看到 revision=1 且当前冲突选择为 right",
               refreshed["revision"] == 1 and current_choice == "right")
    second = call(origin, route + "/decisions",
                  {"revision": 1, "item": conflict["id"], "choice": "left"})
    check.that("第二组持 revision=1 重新提交 left 被接受，版本推进到 2，覆盖第一组选择",
               second["revision"] == 2 and second["choice"] == "left")
    step("第二组刷新后重新提交（revision=1 → 接受，覆盖）",
         {"observed_revision": 1, "observed_choice": "right",
          "submitted_revision": 1, "choice": "left", "accepted": True, "new_revision": 2})

    # 6. 同值幂等
    again = call(origin, route + "/decisions",
                 {"revision": 2, "item": conflict["id"], "choice": "left"})
    check.that("同值重复提交幂等，版本保持 2", again["revision"] == 2)

    # 7. 重演与封存
    final_revision = call(origin, route)["revision"]
    attempt = call(origin, route + "/rehearse", {"revision": final_revision})
    check.that("revision=2 重演通过，changed=3（两条自动行 + 一条人工裁决行）",
               attempt["status"] == "passed" and attempt["changed"] == 3,
               json.dumps(attempt))
    check.that("候选库 SHA-256 与固定预期一致",
               attempt["candidate_digest"] == EXPECTED["candidate_digest"],
               attempt["candidate_digest"])
    step("重演 revision=2", {"status": attempt["status"], "changed": attempt["changed"],
                             "candidate_digest": attempt["candidate_digest"]})

    not_published = call(origin, route + "/download", status=404)
    check.that("封存前下载被 404 unpublished 拒绝", not_published["code"] == "unpublished")
    certificate = call(origin, route + "/seal", {"revision": final_revision})
    check.that("封存凭证绑定计划/裁决/模式/候选四个摘要",
               certificate["revision"] == 2
               and certificate["decision_digest"] == EXPECTED["decision_digest"]
               and certificate["plan_digest"] == EXPECTED["plan_digest"]
               and certificate["schema"] == EXPECTED["schema_digest"]
               and certificate["candidate_digest"] == EXPECTED["candidate_digest"],
               json.dumps(certificate, ensure_ascii=False))
    sealed_edit = call(origin, route + "/decisions",
                       {"revision": final_revision, "item": conflict["id"], "choice": "right"}, 409)
    check.that("封存后再提交裁决被 409 closed_plan 拒绝", sealed_edit["code"] == "closed_plan")
    step("封存并停止编辑", {"certificate": certificate, "sealed_edit_error": sealed_edit["code"]})

    candidate = call(origin, route + "/download")
    check.that("下载字节的 SHA-256 与凭证 candidate_digest 相等",
               hashlib.sha256(candidate).hexdigest() == certificate["candidate_digest"])
    rows = dump_tables(candidate)
    log["final_rows"] = rows
    expected_rows = {
        "bench": [["north", "玄石"], ["south", "右支更名"]],
        "node": [["north", 1, "左支覆写", {"blob_hex": "0014"}, 1.5],
                 ["south", 1, "稳定节点", {"blob_hex": ""}, 3.0],
                 ["south", 2, "左支新增", None, 4.0]],
        "binding": [[1, "north", 1]],
    }
    check.that("最终库恰好包含预期的 6 行（含自动行与人工裁决行）", rows == expected_rows,
               json.dumps(rows, ensure_ascii=False))
    log["certificate"] = certificate
    log["attempts_history"] = call(origin, route + "/attempts")
    return log


def main():
    parser = argparse.ArgumentParser(description="Pagefold 两人裁决演习操作版序")
    parser.add_argument("--origin", help="已运行服务的地址，缺省则自建临时服务")
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("evidence"))
    args = parser.parse_args()
    check = Check()
    if args.origin:
        log = run(args.origin.rstrip("/"), check)
    else:
        from pagefold.server import make_server
        from pagefold.store import Store
        with tempfile.TemporaryDirectory(prefix="pagefold-drill-") as root:
            with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
                thread = Thread(target=server.serve_forever)
                thread.start()
                try:
                    log = run(f"http://127.0.0.1:{server.server_port}", check)
                finally:
                    server.shutdown()
                    thread.join()
    args.out.mkdir(parents=True, exist_ok=True)
    log["checks"] = check.report()
    output = args.out / "drill-evidence.json"
    output.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    print(f"\n核对结果：通过 {len(check.passed)} 项，失败 {len(check.failed)} 项")
    print(f"证据已写入 {output}")
    raise SystemExit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
