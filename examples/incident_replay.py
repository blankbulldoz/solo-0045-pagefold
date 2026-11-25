"""值班排查复现：同一计划「右支通过 → 改选祖本后封存被挡」的时序矩阵。

用真实 HTTP 入口（与生产同一条 server/api 代码路径）构造多条候选时序，
把每一步的请求/响应和最终状态库快照落盘，供接班人核对：

  A 真实改选   right 通过 → base 成功写入（rev 1→2），rev2 未重演
  B 重试混淆   right 通过 → base 成功（rev 1→2）→ 带 rev1 的 base 重试被 409
  C 幂等重试   right 通过 → right 旧版序重试被 409 → right 新版序幂等，不推进版本
  D 归档之后   right 通过 → base 成功 → 归档 → 归档后继续写入（被 409 closed_plan）
  E 新版重演   同 A，另外对 rev2 发起一次重演（样例数据通过，历史多一行）
  F 恢复路径   同 E，再封存 rev2，证明哪一版可以封存
  G 受阻重演   stress 镜像撞唯一约束，attempt 为 blocked 且候选为 NULL，仍不可封存

关键事实由脚本断言：
  * 重演记录按 (plan_id, revision) 固定，裁决改版后旧「通过」记录原样保留；
  * 封存只认当前 revision 的通过候选，旧记录在页面上仍可见但不可封存；
  * 失败的写请求（stale_revision / closed_plan）不产生任何状态库痕迹，
    因此 A 与 B 的最终状态库逐表一致，仅靠状态库无法区分。

用法：
  python3 -m examples.incident_replay [输出目录]
不指定输出目录时在系统临时目录下新建 incident-replay-XXXX。
"""

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store


def snapshot(store_path):
    """只读复制状态库中与排查相关的全部事实（不含 BLOB 正文，只留长度/摘要）。"""
    db = sqlite3.connect(store_path)
    db.row_factory = sqlite3.Row
    try:
        plans = [dict(r) for r in db.execute(
            "SELECT id,revision,state,decisions FROM plans ORDER BY rowid")]
        attempts = [{"plan_id": r["plan_id"], "revision": r["revision"],
                     "result": {k: v for k, v in json.loads(r["result"]).items() if k != "id"},
                     "candidate_bytes": r["candidate_bytes"]}
                    for r in db.execute(
                        "SELECT plan_id,revision,result,length(candidate) AS candidate_bytes "
                        "FROM attempts ORDER BY plan_id,revision")]
        releases = []
        for r in db.execute(
                "SELECT plan_id,certificate,length(candidate) AS candidate_bytes "
                "FROM releases ORDER BY plan_id"):
            certificate = {k: v for k, v in json.loads(r["certificate"]).items() if k != "plan"}
            releases.append({"plan_id": r["plan_id"], "certificate": certificate,
                             "candidate_bytes": r["candidate_bytes"]})
        return {"plans": plans, "attempts": attempts, "releases": releases}
    finally:
        db.close()


class Recorder:
    """走真实 HTTP 入口的客户端，把每次请求和响应（含错误体）抄录到文件。"""

    def __init__(self, out_dir, name):
        self.origin = None
        self.log = out_dir / f"{name}.http.log"
        self.name = name
        self._lines = [f"# 时序 {name} 的完整 HTTP 抄录\n"]

    def call(self, method, route, data=None, expect=None):
        body = None
        headers = {}
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                raw, status = response.read(), response.status
                kind = response.headers.get_content_type()
        except HTTPError as error:
            with error:
                raw, status = error.read(), error.code
                kind = error.headers.get_content_type()
        parsed = json.loads(raw) if kind == "application/json" else raw
        self._lines += [
            f"\n## {method} {route}",
            f"请求体: {json.dumps(data, ensure_ascii=False) if data is not None else '-'}",
            f"响应: HTTP {status} {kind}",
            f"响应体: {raw.decode('utf-8', 'replace')[:2000]}",
        ]
        if expect is not None:
            assert status == expect, f"{route}: 期望 {expect}，实际 {status}: {raw[:300]}"
        return status, parsed

    def close(self):
        self.log.write_text("\n".join(self._lines) + "\n", encoding="utf-8")


def bootstrap(rec):
    """登记 quartz/cerulean/umber 并创建样例计划，返回 (plan_id, conflict_item_id)。"""
    ids = {}
    for flavor in ("quartz", "cerulean", "umber"):
        request = Request(rec.origin + "/api/images", data=image(flavor),
                          headers={"X-Image-Name": flavor,
                                   "Content-Type": "application/octet-stream"}, method="POST")
        with urlopen(request, timeout=10) as response:
            ids[flavor] = json.loads(response.read())
        rec._lines += [f"\n## POST /api/images ({flavor})", "响应: HTTP 201 镜像登记成功"]
    sources = {"base": ids["quartz"]["id"], "left": ids["cerulean"]["id"],
               "right": ids["umber"]["id"]}
    _, plan = rec.call("POST", "/api/plans", sources, 201)
    plan_id = plan["id"]
    conflict = next(item["id"] for item in plan["items"] if item["automatic"] is None)
    return plan_id, conflict


def decide(rec, plan_id, revision, item_id, choice, expect=200):
    return rec.call("POST", f"/api/plans/{plan_id}/decisions",
                    {"revision": revision, "item": item_id, "choice": choice}, expect)


def rehearse(rec, plan_id, revision, expect=200):
    return rec.call("POST", f"/api/plans/{plan_id}/rehearse", {"revision": revision}, expect)


def seal(rec, plan_id, revision, expect=200):
    return rec.call("POST", f"/api/plans/{plan_id}/seal", {"revision": revision}, expect)


def archive(rec, plan_id, revision, expect=200):
    return rec.call("POST", f"/api/plans/{plan_id}/archive", {"revision": revision}, expect)


def open_with_right_pass(rec):
    """共同前缀：rev0 → right(rev1) → rev1 重演通过。"""
    plan_id, conflict = bootstrap(rec)
    _, decision = decide(rec, plan_id, 0, conflict, "right")
    assert decision["revision"] == 1
    _, passed = rehearse(rec, plan_id, 1)
    assert passed["status"] == "passed"
    return plan_id, conflict


def scenario_a(rec):
    """真实改选：base 写入成功，rev2 没有重演，封存被 rehearsal_required 挡住。"""
    plan_id, conflict = open_with_right_pass(rec)
    decide(rec, plan_id, 1, conflict, "base")
    _, plan = rec.call("GET", f"/api/plans/{plan_id}")
    assert plan["revision"] == 2 and plan["state"] == "open"
    _, history = rec.call("GET", f"/api/plans/{plan_id}/attempts")
    assert len(history) == 1 and history[0]["revision"] == 1 and history[0]["status"] == "passed"
    _, problem = seal(rec, plan_id, 2, expect=409)
    assert problem["code"] == "rehearsal_required"
    # 页面证据：封存按钮 disabled，历史里 rev1 标注「历史版」
    _, page = rec.call("GET", f"/plans/{plan_id}")
    html = page.decode()
    assert 'action="/plans/' + plan_id + '/seal"' in html and "disabled" in html
    assert "历史版".encode() in page and "通过".encode() in page
    return plan_id


def scenario_b(rec):
    """重试混淆：成功改选之后又补发带旧版序的 base 重试；最终状态与 A 相同。"""
    plan_id, conflict = open_with_right_pass(rec)
    decide(rec, plan_id, 1, conflict, "base")
    # 第二条「base 写入」其实是浏览器/同事重发的旧表单：版序仍是 1，被 409 拒绝
    _, retry = decide(rec, plan_id, 1, conflict, "base", expect=409)
    assert retry["code"] == "stale_revision"
    _, problem = seal(rec, plan_id, 2, expect=409)
    assert problem["code"] == "rehearsal_required"
    return plan_id


def scenario_c(rec):
    """幂等重试：始终是 right；同版重复不推进版本，rev1 通过记录仍可封存（对照）。"""
    plan_id, conflict = open_with_right_pass(rec)
    _, stale = decide(rec, plan_id, 0, conflict, "right", expect=409)
    assert stale["code"] == "stale_revision"
    _, same = decide(rec, plan_id, 1, conflict, "right")
    assert same["revision"] == 1
    _, plan = rec.call("GET", f"/api/plans/{plan_id}")
    assert plan["revision"] == 1
    seal(rec, plan_id, 1)
    return plan_id


def scenario_d(rec):
    """归档之后：base 改选后归档，归档后的写入全部 closed_plan，封存同样被挡。"""
    plan_id, conflict = open_with_right_pass(rec)
    decide(rec, plan_id, 1, conflict, "base")
    archive(rec, plan_id, 2)
    _, late_write = decide(rec, plan_id, 2, conflict, "right", expect=409)
    assert late_write["code"] == "closed_plan"
    _, late_rehearse = rehearse(rec, plan_id, 2, expect=409)
    assert late_rehearse["code"] == "closed_plan"
    _, late_seal = seal(rec, plan_id, 2, expect=409)
    assert late_seal["code"] == "closed_plan"
    _, plan = rec.call("GET", f"/api/plans/{plan_id}")
    assert plan["state"] == "archived" and plan["revision"] == 2
    return plan_id


def scenario_e(rec):
    """受阻重演对照：rev2 发起重演，页面历史会多出一条 rev2 记录（样例数据可通过）。"""
    plan_id, conflict = open_with_right_pass(rec)
    decide(rec, plan_id, 1, conflict, "base")
    _, second = rehearse(rec, plan_id, 2)
    _, history = rec.call("GET", f"/api/plans/{plan_id}/attempts")
    assert [a["revision"] for a in history] == [2, 1]
    return plan_id, second["status"]


def scenario_f(rec):
    """恢复路径：rev2 重演通过后封存成功；rev1 候选与 rev2 候选是两份不同字节。"""
    plan_id, conflict = open_with_right_pass(rec)
    decide(rec, plan_id, 1, conflict, "base")
    _, current = rehearse(rec, plan_id, 2)
    assert current["status"] == "passed"
    _, certificate = seal(rec, plan_id, 2)
    assert certificate["revision"] == 2
    _, plan = rec.call("GET", f"/api/plans/{plan_id}")
    assert plan["state"] == "sealed"
    return plan_id


def scenario_g(rec):
    """当前版重演受阻：留下 blocked 记录（无候选），同版重演复用该行，封存仍被挡。"""
    # 用 stress 镜像建计划：两项都是自动采纳，没有手动冲突；
    # 但两个 stress 分支各自插入的 bench 行在合并后撞唯一约束，rev0 重演即受阻。
    ids = {}
    for flavor in ("quartz", "stress-left", "stress-right"):
        request = Request(rec.origin + "/api/images", data=image(flavor),
                          headers={"X-Image-Name": flavor,
                                   "Content-Type": "application/octet-stream"}, method="POST")
        with urlopen(request, timeout=10) as response:
            ids[flavor] = json.loads(response.read())
    _, plan = rec.call("POST", "/api/plans",
                       {"base": ids["quartz"]["id"], "left": ids["stress-left"]["id"],
                        "right": ids["stress-right"]["id"]}, 201)
    plan_id = plan["id"]
    _, blocked = rehearse(rec, plan_id, 0)
    assert blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint"
    # 同版再次重演复用同一条受阻记录（attempts 以 plan_id+revision 为主键，不新增行）
    _, cached = rehearse(rec, plan_id, 0)
    assert cached["status"] == "blocked"
    _, problem = seal(rec, plan_id, 0, expect=409)
    assert problem["code"] == "rehearsal_required"
    _, history = rec.call("GET", f"/api/plans/{plan_id}/attempts")
    assert len(history) == 1 and history[0]["status"] == "blocked"
    return plan_id


SCENARIOS = {
    "A_real_change": scenario_a,
    "B_retried_write": scenario_b,
    "C_idempotent_right": scenario_c,
    "D_after_archive": scenario_d,
    "E_rehearse_rev2": scenario_e,
    "F_recover_and_seal": scenario_f,
    "G_blocked_rehearsal": scenario_g,
}


def main():
    parser = argparse.ArgumentParser(description="复现封存被挡的候选时序并落盘排查证据")
    parser.add_argument("out_dir", nargs="?", help="HTTP 抄录与状态快照输出目录")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else Path(
        tempfile.mkdtemp(prefix="incident-replay-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries = {}
    for name, runner in SCENARIOS.items():
        state_path = out_dir / f"state-{name}.sqlite"
        rec = Recorder(out_dir, name)
        with make_server(Store(state_path), 0) as server:
            rec.origin = f"http://127.0.0.1:{server.server_port}"
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                result = runner(rec)
            finally:
                server.shutdown()
                thread.join()
        rec.close()
        snap = snapshot(state_path)
        (out_dir / f"snapshot-{name}.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries[name] = {"plan_result": result, "snapshot": snap}

    # A 与 B 的逐表等价性核对（剔除每次运行变化的 plan_id 后比较）
    def normalize(snap):
        return json.dumps(snap, sort_keys=True, ensure_ascii=False)
    a = summaries["A_real_change"]["snapshot"]
    b = summaries["B_retried_write"]["snapshot"]
    plan_ids = {"id", "plan_id"}
    for key in ("plans", "attempts", "releases"):
        stripped_a = [{k: v for k, v in row.items() if k not in plan_ids} for row in a[key]]
        stripped_b = [{k: v for k, v in row.items() if k not in plan_ids} for row in b[key]]
        assert normalize(stripped_a) == normalize(stripped_b), f"A/B 在 {key} 上状态不一致"
    e_status = summaries["E_rehearse_rev2"]["plan_result"][1]

    report = {
        "output_dir": str(out_dir),
        "A_equals_B_in_state_db": True,
        "E_rev2_rehearse_status": e_status,
        "scenarios": {
            name: {
                "plans": snap["snapshot"]["plans"],
                "attempt_rows": len(snap["snapshot"]["attempts"]),
                "release_rows": len(snap["snapshot"]["releases"]),
            } for name, snap in summaries.items()
        },
    }
    (out_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n证据目录: {out_dir}")
    print("每个时序一份 .http.log（完整请求/响应抄录）和一份 snapshot-*.json（状态库事实）。")


if __name__ == "__main__":
    main()
