"""R027 演示：把封存确认绑定到重演证据。

构造两份确实得到逐字节相同候选、但三方角色安排不同的计划（左右支互换，
冲突选择镜像到对应角色），随后在真实 HTTP 入口上演示：

1. 封存前界面清楚展示候选摘要、版序与来源，并要求明确勾选确认；
2. 指向另一计划的确认（两计划版序、摘要恰好相同也能识别）；
3. 过期确认及其返回的“新比较对象”（JSON 与 HTML 对照页）；
4. 封存成功但响应丢失后的原样重试（重认同一份封存记录）；
5. 直接核对状态库中的封存记录/凭证，以及两份下载字节的一致性与可打开性。

用法：
    python3 -m examples.seal_confirmation_demo
    python3 -m examples.seal_confirmation_demo --out /tmp/r027 --port 8760
"""

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pagefold.codec import digest
from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

ROLE_NAMES = ("base", "left", "right")


def check(condition, description):
    if not condition:
        raise AssertionError(description)
    print(f"  ✓ {description}")


class Http:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def send(self, method, route, data=None, raw=None, content_type="application/json", headers=None):
        request_headers = dict(headers or {})
        body = None
        if raw is not None:
            body = raw
            request_headers["Content-Type"] = content_type
        elif data is not None:
            if content_type == "application/json":
                body = json.dumps(data).encode("utf-8")
                request_headers["Content-Type"] = "application/json"
            else:
                body = urlencode(data).encode("utf-8")
                request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(self.origin + route, data=body, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, response.read(), dict(response.headers)
        except HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def json(self, method, route, data=None, status=200):
        code, body, _ = self.send(method, route, data=data)
        if code != status:
            raise AssertionError(f"{method} {route} 应返回 {status}，实际 {code}：{body[:300]!r}")
        return json.loads(body.decode("utf-8"))

    def expect_problem(self, method, route, data, code_name):
        result = self.json(method, route, data, status=409)
        check(result["code"] == code_name, f"错误码应为 {code_name}，实际 {result['code']}")
        return result

    def html(self, route):
        code, body, _ = self.send("GET", route)
        if code != 200:
            raise AssertionError(f"GET {route} 应返回 200，实际 {code}：{body[:300]!r}")
        return body.decode("utf-8")

    def form(self, route, fields):
        return self.send("POST", route, data=fields, content_type="form")

    def upload(self, name):
        code, body, _ = self.send(
            "POST", "/api/images", raw=image(name), content_type="application/octet-stream",
            headers={"X-Image-Name": name})
        if code != 201:
            raise AssertionError(f"上传 {name} 失败：{code} {body[:200]!r}")
        return json.loads(body.decode("utf-8"))["id"]


def conflict_item(plan):
    return next(item for item in plan["items"] if not item["automatic"])


def decide_and_rehearse(http, plan_id, item_id, choice, revision):
    result = http.json("POST", f"/api/plans/{plan_id}/decisions",
                      {"revision": revision, "item": item_id, "choice": choice})
    next_revision = result["revision"]
    attempt = http.json("POST", f"/api/plans/{plan_id}/rehearse", {"revision": next_revision})
    check(attempt["status"] == "passed", f"计划 {plan_id[:8]} 重演未通过：{attempt}")
    return next_revision, attempt


def main():
    parser = argparse.ArgumentParser(description="R027 封存确认绑定证据演示")
    parser.add_argument("--out", type=Path, default=Path("demo-artifacts"), help="凭证与下载产物目录")
    parser.add_argument("--state", type=Path, default=None, help="状态库路径（默认临时文件）")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pagefold-r027-")) if args.state is None else args.state.parent
    state_path = args.state or (workdir / "r027-state.sqlite")
    args.out.mkdir(parents=True, exist_ok=True)

    with make_server(Store(state_path), args.port) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        http = Http(server.server_port)
        try:
            run_demo(http, state_path, args.out)
        finally:
            server.shutdown()
            thread.join(timeout=5)


def run_demo(http, state_path, out_dir):
    print("① 登记三份镜像，建立两份「同候选、异角色」计划")
    base = http.upload("quartz")
    cerulean = http.upload("cerulean")
    umber = http.upload("umber")
    sources_a = {"base": base, "left": cerulean, "right": umber}
    sources_b = {"base": base, "left": umber, "right": cerulean}

    plan_a = http.json("POST", "/api/plans", sources_a, status=201)
    plan_b = http.json("POST", "/api/plans", sources_b, status=201)
    aid, bid = plan_a["id"], plan_b["id"]
    check(aid != bid, "两份计划编号不同")

    item_a, item_b = conflict_item(plan_a)["id"], conflict_item(plan_b)["id"]
    rev_a, attempt_a = decide_and_rehearse(http, aid, item_a, "left", 0)
    rev_b, attempt_b = decide_and_rehearse(http, bid, item_b, "right", 0)
    digest_v1 = attempt_a["candidate_digest"]
    check(rev_a == rev_b == 1, "两份计划裁决后都处于第 1 版")
    check(attempt_a["candidate_digest"] == attempt_b["candidate_digest"], "两份计划得到相同候选摘要")
    check(attempt_a["size"] == attempt_b["size"], "两份候选大小相同")
    print(f"    计划甲（左=青蓝/右=赤褐）{aid}")
    print(f"    计划乙（左=赤褐/右=青蓝）{bid}")
    print(f"    共同候选 SHA-256 = {digest_v1}（{attempt_a['size']} 字节）")

    print("② 核对封存前界面：证据、来源与明确确认控件")
    preview_a = http.json("GET", f"/api/plans/{aid}/seal")
    preview_b = http.json("GET", f"/api/plans/{bid}/seal")
    check(preview_a["sealable"] and preview_a["confirmation"]["plan"] == aid, "甲的封存证据可读且绑定甲")
    check(preview_b["confirmation"]["sources"] == sources_b, "乙的确认对象绑定乙的角色安排")
    check(preview_a["confirmation"]["candidate_digest"] == digest_v1, "确认对象携带所见摘要")

    page_a = http.html(f"/plans/{aid}")
    page_b = http.html(f"/plans/{bid}")
    for html_text, pid, left_id in ((page_a, aid, cerulean), (page_b, bid, umber)):
        for token in ("封存前证据确认", digest_v1, f'action="/plans/{pid}/seal"',
                      f'name="confirm_plan" value="{pid}"', 'name="confirmed" value="yes"',
                      'data-seal-form', f'name="confirm_left" value="{left_id}"',
                      "确认摘要与来源，完成封存"):
            check(token in html_text, f"计划 {pid[:8]} 界面包含 {token[:48]!r}")
    check(f'name="confirm_left" value="{cerulean}"' in page_a
          and f'name="confirm_left" value="{umber}"' in page_b, "两页隐藏字段的左支角色恰好互换")

    print("③ 区分「指向另一计划的确认」（此时同版、同摘要）")
    cross = http.expect_problem(
        "POST", f"/api/plans/{aid}/seal",
        {"revision": 1, "confirmed": True, "confirmation": preview_b["confirmation"]},
        "confirmation_plan_mismatch")
    check(cross["confirmation_plan"] == bid and cross["plan"] == aid, "错误同时标明确认所属计划与当前计划")
    check(cross["other_plan"]["id"] == bid and cross["other_plan"]["revision"] == 1, "给出另一计划的当前摘要")
    check(cross["current"]["candidate_digest"] == digest_v1, "当前计划比较对象摘要正确")

    code, html_body, _ = http.form(f"/plans/{aid}/seal", {
        "revision": "1", "confirmed": "yes", "confirm_plan": bid, "confirm_revision": "1",
        "confirm_digest": digest_v1, f"confirm_base": base,
        "confirm_left": sources_b["left"], "confirm_right": sources_b["right"]})
    cross_html = html_body.decode("utf-8")
    check(code == 409, f"表单跨计划确认应被拒绝，实际 {code}")
    check("确认指向另一个计划" in cross_html and f"/plans/{bid}" in cross_html,
          "HTML 错误页说明跨计划并提供另一计划链接")

    print("④ 制造过期确认，并核对返回的「新比较对象」")
    changed = http.json("POST", f"/api/plans/{aid}/decisions",
                        {"revision": 1, "item": item_a, "choice": "base"})
    check(changed["revision"] == 2, "改选祖本推进到第 2 版")
    attempt_a2 = http.json("POST", f"/api/plans/{aid}/rehearse", {"revision": 2})
    digest_v2 = attempt_a2["candidate_digest"]
    check(attempt_a2["status"] == "passed" and digest_v2 != digest_v1, "第 2 版产生了不同的新候选")

    stale = http.expect_problem("POST", f"/api/plans/{aid}/seal", {
        "revision": 2, "confirmed": True,
        "confirmation": {"plan": aid, "revision": 1, "candidate_digest": digest_v1,
                        "sources": sources_a}}, "stale_confirmation")
    check(stale["confirmed"]["revision"] == 1 and stale["confirmed"]["candidate_digest"] == digest_v1,
          "错误回显操作者曾确认的旧对象")
    check(stale["current"]["revision"] == 2 and stale["current"]["candidate_digest"] == digest_v2,
          "错误携带第 2 版新比较对象及其摘要")
    check(stale["current"]["sources"] == sources_a, "新比较对象仍绑定甲的来源")

    code, html_body, _ = http.form(f"/plans/{aid}/seal", {
        "revision": "1", "confirmed": "yes", "confirm_plan": aid, "confirm_revision": "1",
        "confirm_digest": digest_v1, "confirm_base": base,
        "confirm_left": cerulean, "confirm_right": umber})
    stale_html = html_body.decode("utf-8")
    check(code == 409, f"表单过期确认应被拒绝，实际 {code}")
    check("确认已过期" in stale_html and "新的比较对象" in stale_html, "HTML 显示过期对照标题")
    check(digest_v1 in stale_html and digest_v2 in stale_html, "对照页同时展示旧摘要与新摘要")

    print("⑤ 恢复目标选择，让两份计划再次同版同摘要，并校验确认门槛")
    http.json("POST", f"/api/plans/{aid}/decisions", {"revision": 2, "item": item_a, "choice": "left"})
    back_a = http.json("POST", f"/api/plans/{aid}/rehearse", {"revision": 3})
    http.json("POST", f"/api/plans/{bid}/decisions", {"revision": 1, "item": item_b, "choice": "base"})
    back_b2 = http.json("POST", f"/api/plans/{bid}/rehearse", {"revision": 2})
    http.json("POST", f"/api/plans/{bid}/decisions", {"revision": 2, "item": item_b, "choice": "right"})
    back_b = http.json("POST", f"/api/plans/{bid}/rehearse", {"revision": 3})
    check(back_a["candidate_digest"] == back_b["candidate_digest"] == digest_v1,
          "往返改选后两份计划在第 3 版重新得到同一候选")
    check(back_b2["candidate_digest"] == digest_v2, "乙的第 2 版比较对象与甲的新对象同样一致")

    preview_a3 = http.json("GET", f"/api/plans/{aid}/seal")
    preview_b3 = http.json("GET", f"/api/plans/{bid}/seal")
    conf_a, conf_b = preview_a3["confirmation"], preview_b3["confirmation"]
    check(conf_a["revision"] == conf_b["revision"] == 3 and conf_a["candidate_digest"]
          == conf_b["candidate_digest"] == digest_v1, "两份确认对象再次同版、同摘要，仅计划与角色不同")
    cross2 = http.expect_problem(
        "POST", f"/api/plans/{aid}/seal",
        {"revision": 3, "confirmed": True, "confirmation": conf_b},
        "confirmation_plan_mismatch")
    check(cross2["confirmation_plan"] == bid, "第 3 版下跨计划确认仍被拦截")

    required = http.expect_problem("POST", f"/api/plans/{aid}/seal", {"revision": 3},
                                   "confirmation_required")
    check(required["confirmation"]["candidate_digest"] == digest_v1, "缺少确认时返回可回显的确认对象")
    code, body, _ = http.form(f"/plans/{aid}/seal", {
        "revision": "3", "confirm_plan": aid, "confirm_revision": "3",
        "confirm_digest": digest_v1, "confirm_base": base,
        "confirm_left": cerulean, "confirm_right": umber})
    unchecked_html = body.decode("utf-8")
    check(code == 409 and "明确确认" in unchecked_html, "未勾选的表单确认不生效")

    print("⑥ 完成封存，并模拟「成功但响应丢失」的原样重试")
    cert_a = http.json("POST", f"/api/plans/{aid}/seal",
                       {"revision": 3, "confirmed": True, "confirmation": conf_a})
    retry_a = http.json("POST", f"/api/plans/{aid}/seal",
                        {"revision": 3, "confirmed": True, "confirmation": conf_a})
    check(retry_a == cert_a, "甲的同确认重试返回完全相同的凭证")
    code, form_body, _ = http.form(f"/plans/{aid}/seal", {
        "revision": "3", "confirmed": "yes", "confirm_plan": aid, "confirm_revision": "3",
        "confirm_digest": digest_v1, "confirm_base": base,
        "confirm_left": cerulean, "confirm_right": umber})
    check(code == 200 and "封存记录与凭证" in form_body.decode("utf-8"),
          "甲的表单幂等重试回到展示封存记录的计划页")

    cert_b = http.json("POST", f"/api/plans/{bid}/seal",
                       {"revision": 3, "confirmed": True, "confirmation": conf_b})
    retry_b = http.json("POST", f"/api/plans/{bid}/seal",
                        {"revision": 3, "confirmed": True, "confirmation": conf_b})
    check(retry_b == cert_b, "乙的同确认重试返回完全相同的凭证")
    check(cert_a["plan"] == aid and cert_b["plan"] == bid, "凭证分别绑定各自计划编号")
    check(cert_a["sources"] == sources_a and cert_b["sources"] == sources_b, "凭证保留不同角色来源")
    check(cert_a["candidate_digest"] == cert_b["candidate_digest"] == digest_v1,
          "两份凭证冻结的候选摘要相同")
    check(json.dumps(cert_a, sort_keys=True) != json.dumps(cert_b, sort_keys=True),
          "两份凭证整体并不相同（计划与来源不同）")
    after_seal_cross = http.expect_problem(
        "POST", f"/api/plans/{aid}/seal",
        {"revision": 3, "confirmed": True, "confirmation": conf_b},
        "confirmation_plan_mismatch")
    check(after_seal_cross["plan"] == aid, "封存后跨计划确认仍被拒绝而非回吐甲的凭证")

    print("⑦ 直接核对状态库中的封存记录（每个计划恰好一条）")
    with sqlite3.connect(state_path) as db:
        states = dict(db.execute("SELECT id,state FROM plans").fetchall())
        check(states[aid] == states[bid] == "sealed", "两个计划状态均为 sealed")
        release_rows = db.execute("SELECT plan_id,certificate,candidate FROM releases ORDER BY plan_id").fetchall()
        check([row[0] for row in release_rows].count(aid) == 1
              and [row[0] for row in release_rows].count(bid) == 1 and len(release_rows) == 2,
              "releases 中每个计划恰好一条封存记录，重试未产生重复")
        stored = {}
        for plan_id, certificate_text, candidate_blob in release_rows:
            certificate = json.loads(certificate_text)
            check(certificate["candidate_digest"] == digest(candidate_blob),
                  f"{plan_id[:8]} 封存字节的 SHA-256 与凭证一致")
            check(certificate["revision"] == 3, f"{plan_id[:8]} 封存记录绑定第 3 版")
            stored[plan_id] = (certificate, candidate_blob)
    check(stored[aid][1] == stored[bid][1], "两条封存记录保存的候选字节逐字节相同")

    print("⑧ 核对下载结果：HTTP 字节、凭证、SQLite 独立打开与行内容")
    code, download_a, headers_a = http.send("GET", f"/api/plans/{aid}/download")
    code_b, download_b, headers_b = http.send("GET", f"/api/plans/{bid}/download")
    check(code == 200 and code_b == 200, "两个下载入口均返回 200")
    check(digest(download_a) == digest_v1 and digest(download_b) == digest_v1, "下载字节摘要等于凭证摘要")
    check(download_a == download_b == stored[aid][1] == stored[bid][1],
          "两次下载与库内冻结字节完全一致（异角色、同候选）")
    check(aid in headers_a.get("Content-Disposition", "") and ".sqlite" in headers_a.get("Content-Disposition", ""),
          "SQLite 下载文件名绑定计划编号")
    cert_route_a = http.json("GET", f"/api/plans/{aid}/certificate")
    check(cert_route_a == cert_a, "凭证下载内容与封存响应一致")

    verification = sqlite3.connect(":memory:")
    try:
        verification.deserialize(download_a)
        check(verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "下载库 integrity_check = ok")
        check(verification.execute("PRAGMA foreign_key_check").fetchall() == [], "下载库无外键违规")
        note = verification.execute(
            "SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()[0]
        check(note == "左支覆写", "冲突行内容来自 cerulean（甲选左支 / 乙选右支，殊途同归）")
        check(verification.execute("SELECT label FROM bench WHERE code='south'").fetchone()[0] == "右支更名",
              "单边自动项（umber 的 bench 更名）在两份结果中都保留")
        check(verification.execute("SELECT note FROM node WHERE bench='south' AND slot=2").fetchone()[0] == "左支新增",
              "单边自动项（cerulean 的新增行）在两份结果中都保留")
    finally:
        verification.close()

    print("⑨ 核对封存后界面：封存记录与下载入口取代确认表单")
    sealed_html_a = http.html(f"/plans/{aid}")
    check("封存记录与凭证" in sealed_html_a and "sealed-record" in sealed_html_a, "甲页展示封存记录面板")
    check(digest_v1 in sealed_html_a and f'href="/api/plans/{aid}/download"' in sealed_html_a
          and f'href="/api/plans/{aid}/certificate"' in sealed_html_a, "甲页提供摘要与两个下载入口")
    check("确认摘要与来源，完成封存" not in sealed_html_a, "甲页不再显示待确认表单")

    (out_dir / "plan-a.sqlite").write_bytes(download_a)
    (out_dir / "plan-b.sqlite").write_bytes(download_b)
    (out_dir / "certificate-a.json").write_text(
        json.dumps(cert_a, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "certificate-b.json").write_text(
        json.dumps(cert_b, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"plan_a": aid, "plan_b": bid, "revision": 3, "candidate_digest": digest_v1,
               "candidate_size": attempt_a["size"], "sources_a": sources_a, "sources_b": sources_b}
    (out_dir / "demonstration-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物已保存到：{out_dir.resolve()}")
    print("全部核对通过：界面确认、过期/跨计划/重试三类区分、封存记录与下载字节均符合预期。")


if __name__ == "__main__":
    main()
