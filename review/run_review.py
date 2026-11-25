"""对四组评审样本执行真实 HTTP 操作并记录观察（供评审会议直接使用）。

前置：python3 -m review.make_samples
用法：python3 -m review.run_review
输出：review/observations.json（原始记录）与 review/OBSERVATIONS.md（会议版）

流程：临时状态库 + 随机端口的真实 Pagefold HTTP 服务；每组依次
上传 → 建计划 → （未决拦截）→ 裁决 → （过期/幂等检查）→ 重演 → 封存 → 下载校验，
并把 pagefold 计划条目与 make_samples 的独立差异表交叉核对。
"""

import hashlib
import json
import platform
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.server import make_server
from pagefold.store import Store
from review.groups import GROUPS

ROOT = Path(__file__).resolve().parent


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None):
        request_headers = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=request_headers)
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=120) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        elapsed = (time.perf_counter() - started) * 1000
        if actual != status:
            raise RuntimeError(f"{route}: {actual} != {status}: {body[:300]}")
        parsed = json.loads(body) if kind == "application/json" else body
        return parsed, round(elapsed, 1), len(body)

    def upload(self, label, data):
        return self.call("/api/images", data, 201,
                         {"X-Image-Name": label, "Content-Type": "application/octet-stream"})


def key_tuple(item):
    values = [int(atom[1]) if atom[0] == "i" else atom[1] for atom in item["key"]]
    return item["table"], tuple(values)


def run_group(client, spec, meta):
    gid = spec["id"]
    expect = spec["expect"]
    obs = {"group": gid, "title": spec["title"], "purpose": spec["purpose"],
           "checks": [], "notes": []}

    def check(label, condition):
        obs["checks"].append(("✓ " if condition else "✗ ") + label)
        if not condition:
            raise RuntimeError(f"{gid}: 校验失败：{label}")

    uploads = {}
    for role in ("base", "left", "right"):
        data = (ROOT.parent / meta["files"][role]["path"]).read_bytes()
        record, ms, _ = client.upload(f"{gid}/{role}", data)
        check(f"{role} 镜像登记，摘要与清单一致", record["id"] == meta["files"][role]["sha256"])
        uploads[role] = {"id": record["id"], "ms": ms}
    obs["uploads"] = uploads

    sources = {role: uploads[role]["id"] for role in ("base", "left", "right")}
    plan, ms, _ = client.call("/api/plans", sources, 201)
    route = f"/api/plans/{plan['id']}"
    items = plan["items"]
    automatic = sum(1 for item in items if item["automatic"])
    check(f"计划条目 {len(items)} ＝ 独立差异表 {expect['items']}", len(items) == expect["items"])
    check(f"自动采纳 {automatic} ＝ 独立差异表 {expect['automatic']}", automatic == expect["automatic"])
    check(f"待裁决 {plan['unresolved']} ＝ 独立差异表 {expect['conflicts']}",
          plan["unresolved"] == expect["conflicts"])
    _, _, plan_bytes = client.call(route)
    obs["plan"] = {"id": plan["id"], "ms": ms, "items": len(items), "automatic": automatic,
                   "unresolved": plan["unresolved"], "digest": plan["plan_digest"],
                   "payload_bytes": plan_bytes}

    revision = 0
    if expect["conflicts"]:
        problem, _, _ = client.call(route + "/rehearse", {"revision": 0}, 409)
        check("存在未决项时重演被拦截（409 unresolved）", problem["code"] == "unresolved")
        obs["blocked_rehearsal"] = problem["code"]

    conflicts = {key_tuple(item): item for item in items if not item["automatic"]}
    decisions_log = []
    for want_key, choice in sorted(spec["decisions"].items()):
        item = conflicts[want_key]
        result, _, _ = client.call(route + "/decisions",
                                   {"revision": revision, "item": item["id"], "choice": choice})
        revision = result["revision"]
        decisions_log.append({"table": want_key[0], "key": list(want_key[1]),
                              "choice": choice, "revision": revision})
    obs["decisions"] = decisions_log
    if decisions_log:
        first, last = decisions_log[0], decisions_log[-1]
        first_item = conflicts[(first["table"], tuple(first["key"]))]
        last_item = conflicts[(last["table"], tuple(last["key"]))]
        problem, _, _ = client.call(route + "/decisions",
                                    {"revision": 0, "item": first_item["id"],
                                     "choice": first["choice"]}, 409)
        check("过期版本裁决被拒绝（409 stale_revision）", problem["code"] == "stale_revision")
        again, _, _ = client.call(route + "/decisions",
                                  {"revision": revision, "item": last_item["id"],
                                   "choice": last["choice"]})
        check("同值重复裁决不推进版本（幂等）", again["revision"] == revision)

    result, ms, _ = client.call(route + "/rehearse", {"revision": revision})
    check("重演通过（status=passed）", result["status"] == "passed")
    check(f"重演变更 {result['changed']} ＝ 预期 {expect['changed']}",
          result["changed"] == expect["changed"])
    again, reuse_ms, _ = client.call(route + "/rehearse", {"revision": revision})
    check("同版本重演复用首次证据", again == result)
    obs["rehearse"] = {"ms": ms, "reuse_ms": reuse_ms, "changed": result["changed"],
                       "candidate_bytes": result["size"], "digest": result["candidate_digest"]}

    certificate, _, _ = client.call(route + "/seal", {"revision": revision})
    again, _, _ = client.call(route + "/seal", {"revision": revision})
    check("重复封存结果稳定", again == certificate)
    check("凭证绑定三个来源", certificate["sources"] == sources)
    obs["certificate"] = certificate

    candidate, _, _ = client.call(route + "/download")
    check("下载摘要与封存凭证一致",
          hashlib.sha256(candidate).hexdigest() == certificate["candidate_digest"])
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        integrity = db.execute("PRAGMA integrity_check(1)").fetchone()[0]
        check("候选 integrity_check 通过", integrity == "ok")
        check("候选无外键违规", db.execute("PRAGMA foreign_key_check").fetchall() == [])
        for table, want in expect["rows"].items():
            got = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            check(f"候选 {table} 行数 {got} ＝ 预期 {want}", got == want)
        for sql, want in spec["content_checks"]:
            got = db.execute(sql).fetchone()[0]
            check(f"候选内容 {sql} → {got!r}", got == want)
    finally:
        db.close()

    if spec.get("archive"):
        client.call(route + "/archive", {"revision": revision})
        state, _, _ = client.call(route)
        check("归档后状态为 archived", state["state"] == "archived")
        proof, _, _ = client.call(route + "/certificate")
        check("归档后凭证保持可提取且不变", proof == certificate)
    return obs


def group_notes(obs, meta):
    """把记录整理成会议可直接引用的观察要点。"""
    gid, plan, rehearse = obs["group"], obs["plan"], obs["rehearse"]
    rows_in = sum(f["rows"]["entry"] + f["rows"]["tag"] for f in meta["files"].values())
    bytes_in = sum(f["bytes"] for f in meta["files"].values())
    identical = meta["diff"]["totals"]["identical"]
    notes = []
    if gid == "a-small-conflict":
        notes.append(f"未裁决直接重演被 409（unresolved）拦截；过期版本裁决被 409"
                     f"（stale_revision）拒绝；同值重复裁决不推进版本。")
        notes.append(f"重演变更 {rehearse['changed']} 行 ＝ 计划条目 {plan['items']}："
                     f"本组无空转项；封存后归档，凭证与候选摘要保持不变。")
    elif gid == "b-dense-auto":
        notes.append(f"{plan['items']:,} 条计划全部自动采纳、零冲突：左支 1,400 项"
                     f"（1,000 改 ＋ 200 增及其标签），右支 100 项。")
        notes.append(f"同版本重复重演复用首次证据：首次 {rehearse['ms']} ms，"
                     f"复用 {rehearse['reuse_ms']} ms。")
        notes.append(f"计划响应 {plan['payload_bytes']:,} 字节 / {plan['items']:,} 条，"
                     f"条目数随变更行数线性增长。")
    elif gid == "c-bulk-identical":
        notes.append(f"三份输入合计 {rows_in:,} 行 / {bytes_in:,} 字节，计划仅 "
                     f"{plan['items']} 条：{identical:,} 行三方相同被跳过，未计入变更。")
        notes.append(f"计划响应仅 {plan['payload_bytes']:,} 字节，与输入体积无比例关系；"
                     f"计划创建 {plan['ms']} ms、重演 {rehearse['ms']} ms"
                     f"（全量扫描与校验的成本随输入行数走，不随条目数走）。")
    elif gid == "d-noop-decisions":
        notes.append(f"{plan['items']} 条计划（6 冲突 ＋ 4 自动），6 项裁决中 4 项选祖本："
                     f"重演变更 {rehearse['changed']} 行 ＜ 计划条目 {plan['items']}，"
                     f"祖本裁决不产生实际变更。")
        notes.append(f"下载候选中 entry 1–4 保持祖本值，entry 5 为左支值、entry 6 为右支值；"
                     f"凭证 decision_digest 绑定全部 6 项裁决。")
    notes.append(f"候选 {rehearse['candidate_bytes']:,} 字节：摘要与凭证一致，"
                 f"integrity_check 通过、无外键违规、行数与内容抽样全部符合预期。")
    return notes


def environment():
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                         cwd=ROOT.parent, text=True).strip()
    except Exception:
        commit = "(未知)"
    return {"date": f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "commit": commit}


def render_observations(env, manifest, all_obs):
    lines = [
        "# Pagefold 评审 · 真实操作观察记录",
        "",
        f"- 日期：{env['date']}",
        f"- 环境：Python {env['python']} ／ SQLite {env['sqlite']} ／ 工作区提交 `{env['commit']}`",
        "- 方式：临时状态库 ＋ 随机端口的真实 HTTP 服务；四组样本各自完成"
        " 上传 → 建计划 → 裁决 → 重演 → 封存 → 下载校验 全流程。",
        "- 同批原始记录见 `observations.json`；样本与差异表见 `MANIFEST.md`。",
        "",
        "## 总览",
        "",
        "| 组 | 输入行 | 输入字节 | 计划条目 | 自动 | 冲突 | 重演变更 | 条目/千行 | 候选字节 | 计划耗时 | 重演耗时 |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for obs in all_obs:
        meta = manifest["groups"][obs["group"]]
        rows_in = sum(f["rows"]["entry"] + f["rows"]["tag"] for f in meta["files"].values())
        bytes_in = sum(f["bytes"] for f in meta["files"].values())
        plan, rehearse = obs["plan"], obs["rehearse"]
        per_k = plan["items"] / (rows_in / 1000)
        lines.append(f"| {obs['group']}（{obs['title']}） | {rows_in:,} | {bytes_in:,} "
                     f"| {plan['items']:,} | {plan['automatic']:,} | {plan['unresolved']} "
                     f"| {rehearse['changed']:,} | {per_k:.2f} | {rehearse['candidate_bytes']:,} "
                     f"| {plan['ms']} ms | {rehearse['ms']} ms |")
    lines.append("")
    for obs in all_obs:
        meta = manifest["groups"][obs["group"]]
        lines += [f"## {obs['group']} — {obs['title']}", "", obs["purpose"], "", "观察："]
        lines += [f"- {note}" for note in obs["notes"]]
        lines += ["", f"校验（{len(obs['checks'])} 项，全部通过）："]
        lines += [f"- {item}" for item in obs["checks"]]
        lines.append("")
    return "\n".join(lines)


def main():
    manifest_path = ROOT / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("找不到 manifest.json，请先运行 python3 -m review.make_samples")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="pagefold-review-") as tmp:
        server = make_server(Store(Path(tmp) / "state.sqlite"), 0)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            client = Client(server.server_port)
            all_obs = []
            for spec in GROUPS:
                meta = manifest["groups"][spec["id"]]
                obs = run_group(client, spec, meta)
                obs["notes"] = group_notes(obs, meta)
                all_obs.append(obs)
                plan, rehearse = obs["plan"], obs["rehearse"]
                print(f"{spec['id']}: 条目 {plan['items']}（待裁决 {plan['unresolved']}）→ "
                      f"重演变更 {rehearse['changed']} 行，{len(obs['checks'])} 项校验通过")
        finally:
            server.shutdown()
            thread.join()
    env = environment()
    record = {"generator": "review/run_review.py", "environment": env, "groups": all_obs}
    (ROOT / "observations.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ROOT / "OBSERVATIONS.md").write_text(
        render_observations(env, manifest, all_obs), encoding="utf-8")
    print("已写出 OBSERVATIONS.md 与 observations.json")


if __name__ == "__main__":
    main()
