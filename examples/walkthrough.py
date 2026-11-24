import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from pagefold.demo import image
from pagefold.plans import PAGE_LIMIT
from pagefold.server import make_server
from pagefold.store import Store

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts", "seal",
             "certificate", "download", "archive")


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"
        self.plain = build_opener(NoRedirect)

    def call(self, route, data=None, status=200, headers=None):
        request_headers = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=request_headers)
        try:
            with urlopen(request, timeout=10) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        expect(actual == status, f"{route}: {actual} != {status}: {body[:300]}")
        return json.loads(body) if kind == "application/json" else body

    def submit(self, route, data):
        request = Request(self.origin + route, data=urlencode(data).encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.plain.open(request, timeout=10) as result:
                return result.status, result.headers.get("Location", ""), result.read()
        except HTTPError as error:
            with error:
                return error.code, error.headers.get("Location", ""), error.read()

    def upload(self, name, data=None, status=201):
        return self.call("/api/images", image(name) if data is None else data, status, {
            "X-Image-Name": name, "Content-Type": "application/octet-stream"})


def mismatched_schema():
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(image("quartz"))
        db.execute("CREATE INDEX bench_label_sample ON bench(label)")
        return db.serialize()
    finally:
        db.close()


def query(plan_id, **params):
    return f"/plans/{plan_id}?" + urlencode(params)


def review_paging(client):
    bulk = {role: client.upload(flavor)["id"]
            for role, flavor in (("base", "bulk-quartz"), ("left", "bulk-cerulean"),
                                 ("right", "bulk-umber"))}
    plan = client.call("/api/plans", bulk, 201)
    route = f"/api/plans/{plan['id']}"
    expect(plan["counts"]["pending"] > 2 * PAGE_LIMIT, "示例未超过两页待决行")
    expect(plan["counts"]["automatic"] > 0, "示例没有自动项")
    expect(plan["counts"]["items"] == plan["counts"]["pending"] + plan["counts"]["automatic"], "示例行数不闭合")
    first = client.call(route + "?" + urlencode(
        {"table": "node", "pending": 1, "limit": PAGE_LIMIT, "revision": plan["revision"]}))
    expect(len(first["items"]) == PAGE_LIMIT and first["view"]["pages"] >= 3, "首屏未分页")
    expect(first["view"]["table"] == "node" and first["view"]["pending"], "首屏筛选条件丢失")
    expect(all(item["pending"] for item in first["items"]), "待决视图混入已裁决行")
    expect(first["unresolved"] == plan["counts"]["pending"], "未决数与计划不符")
    second = client.call(route + "?" + urlencode(
        {"table": "node", "pending": 1, "limit": PAGE_LIMIT, "cursor": first["view"]["next"],
         "revision": plan["revision"]}))
    expect(len(second["items"]) == PAGE_LIMIT and second["view"]["page"] == 2, "第二页位置不符")
    expect({item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"]),
           "两页出现重复行")
    expect(second["view"]["table"] == "node" and second["view"]["revision"] == plan["revision"],
           "翻页后读数版本不明")
    first_html = client.call(query(plan["id"], table="node", pending=1, limit=PAGE_LIMIT,
                                   revision=plan["revision"]))
    second_html = client.call(query(plan["id"], table="node", pending=1, limit=PAGE_LIMIT,
                                    cursor=first["view"]["next"], revision=plan["revision"]))
    expect(first_html.count(b'class="entry"') == PAGE_LIMIT, "HTML 首屏行数与 JSON 不符")
    expect(b"node" in first_html and "下一页".encode() in first_html
           and "第 1/3 页".encode() in first_html, "HTML 首屏缺少翻页提示")
    for item in first["items"]:
        expect(f'name="item" value="{item["id"]}"'.encode() in first_html, "HTML 首屏缺少 JSON 中的行")
    for item in second["items"]:
        expect(f'name="item" value="{item["id"]}"'.encode() in second_html, "HTML 第二页缺少 JSON 中的行")
    expect(f"{plan['counts']['pending']} 项待裁决".encode() in second_html, "HTML 未显示未决数")
    expect(f"第 {plan['revision']} 版读取".encode() in second_html, "HTML 未标明读取版本")
    expect("table=node".encode() in second_html and "pending=1".encode() in second_html,
           "翻页链接丢失筛选条件")

    # Save a page-1 decision through the form: 302 back into the paged, pending-only view.
    target = first["items"][0]
    status, location, _ = client.submit(f"/plans/{plan['id']}/decisions", {
        "revision": first["view"]["current_revision"], "item": target["id"], "choice": "left",
        "table": "node", "pending": "1", "limit": PAGE_LIMIT})
    expect(status == 303 and f"saved={target['id']}" in location and "table=node" in location,
           f"保存后未回到当前表: {status} {location}")
    expect("pending=1" in location and f"limit={PAGE_LIMIT}" in location, "保存后丢失筛选条件")
    followed = client.call(location)
    expect("已保存".encode() in followed and "node".encode() in followed, "继续位置页未说明保存结果")
    rendered = re.findall(rb'name="item" value="([a-f0-9]{64})"', followed)
    expect(target["id"].encode() not in rendered, "已裁决行仍留在待决页")
    expect(rendered and rendered[0].decode() == first["items"][1]["id"], "继续位置未从已裁决行之后开始")
    expect("本页".encode() in followed, "继续位置未说明偏移")
    expect(client.call(route)["unresolved"] == plan["counts"]["pending"] - 1, "保存后未决数未减少")
    expect(f"{plan['counts']['pending'] - 1} 项待裁决".encode() in followed, "HTML 未同步未决数")

    # A link opened at revision 0 keeps its stated revision and is flagged stale after a save.
    old = query(plan["id"], table="node", pending=1, limit=PAGE_LIMIT, revision=0)
    stale = client.call(old)
    expect("链接指向第 0 版".encode() in stale and "第 1 版".encode() in stale, "过期页未标注版本")
    expect("以第 1 版重新打开本页".encode() in stale, "过期页缺少新版本入口")
    decision_forms = re.findall(rb'<form class="decision"[^>]*>\s*<input type="hidden" name="revision" value="(\d+)"', stale)
    expect(decision_forms and set(decision_forms) == {b"0"},
           "过期页裁决表单未保留打开时的版本")
    current_rows = client.call(route + "?" + urlencode(
        {"table": "node", "pending": 1, "limit": PAGE_LIMIT}))
    stale_rendered = re.findall(rb'name="item" value="([a-f0-9]{64})"', stale)
    expect(stale_rendered == [item["id"].encode() for item in current_rows["items"]],
           "过期页未按当前版本继续读取")
    expect(f"{current_rows['unresolved']} 项待裁决".encode() in stale, "过期页未显示当前未决数")
    # The same stale link with its old cursor still resolves that position in the current version.
    shifted = client.call(route + "?" + urlencode(
        {"table": "node", "pending": 1, "limit": PAGE_LIMIT,
         "cursor": first["view"]["next"], "revision": 0}))
    expect(shifted["view"]["stale"] and len(shifted["items"]) == PAGE_LIMIT,
           "旧翻页位置未能在当前版本继续读取")
    expect({item["id"] for item in shifted["items"]}.isdisjoint(item["id"] for item in first["items"]),
           "旧翻页位置与首页重叠")
    stale_revision = plan["revision"]
    status, _, body = client.submit(f"/plans/{plan['id']}/decisions", {
        "revision": stale_revision, "item": second["items"][0]["id"], "choice": "right"})
    expect(status == 409 and "版本已改变".encode() in body, "旧页提交未按过期版本拒绝")

    # The saved row is no longer pending, but it stays queryable in the pending list location.
    gone = client.call(route + "?" + urlencode(
        {"table": "node", "pending": 1, "limit": PAGE_LIMIT, "saved": target["id"]}))
    expect(gone["saved"] is not None and gone["saved"]["pending"] is False, "继续位置未标识已裁决行")
    expect(gone["saved"]["choice"] == "left", "继续位置裁决值不符")

    # A pending-only page past the tail of a table is empty yet the plan still reports pending rows.
    bench_rows = [item for item in plan["items"] if item["table"] == "bench"]
    last_bench = bench_rows[-1]["id"]
    tail = client.call(route + "?" + urlencode(
        {"table": "bench", "pending": 1, "limit": PAGE_LIMIT, "cursor": last_bench}))
    expect(tail["items"] == [], "尾页之后仍显示行")
    expect(tail["unresolved"] == plan["counts"]["pending"] - 1, "尾页未决数不一致")
    expect(plan["counts"]["pending"] - 1 > 0, "尾页测试未保留其他表的待决行")

    expect(client.call(route + "?pending=2", status=400)["code"] == "pending", "非法待决筛选未拒绝")
    expect(client.call(route + "?limit=0", status=400)["code"] == "limit", "非法行数未拒绝")
    expect(client.call(route + "?cursor=bad", status=400)["code"] == "cursor", "非法翻页位置未拒绝")
    expect(client.call(route + "?" + urlencode({"table": "missing"}), status=404)["code"] == "unknown_table",
           "未知表未拒绝")
    expect(client.call(route + "?" + urlencode({"limit": 2}))["view"]["pages"] > 2, "小页未产生多页")
    api = client.call(route + "?" + urlencode({"pending": 1, "limit": 2}))
    expect(len(api["items"]) == 2 and api["view"]["pending"], "JSON 未按待决分页")
    plain = client.call(route)
    expect(plain["view"]["limit"] is None and plain["view"]["pending"] is False,
           "不带筛选的 JSON 不应截断")
    expect(len(plain["items"]) == plain["counts"]["items"], "无筛选 JSON 未返回全部行")


def run(client, goal):
    def done(workflow, result):
        if goal in (workflow, "all"):
            print(f"{workflow} ✓ {result}")
        return goal == workflow

    home = client.call("/")
    expect("三方合流工作区".encode() in home, "工作区未渲染")
    base = client.upload("quartz")
    expect(client.upload("cerulean", image("quartz"))["id"] == base["id"], "重复镜像未复用")
    expect(client.upload("quartz", b"broken", 422)["code"] == "image_header", "坏镜像未拒绝")
    if done("import", "有效文件登记，重复文件复用，坏镜像拒绝"):
        return
    left, right = client.upload("cerulean"), client.upload("umber")
    image_route = f"/api/images/{base['id']}"
    catalog = client.call(image_route)["catalog"]
    node = next(table for table in catalog["tables"] if table["name"] == "node")
    expect(node["key"] == ["bench", "slot"] and node["foreign_keys"], "复合主键或外键目录不正确")
    expect(client.call("/api/images/missing", status=404)["code"] == "unknown_image", "未知镜像未拒绝")
    page = client.call(f"/images/{base['id']}")
    expect("关系结构".encode() in page, "镜像目录未渲染")
    if done("catalog", "目录保留复合主键顺序及依赖"):
        return
    rows = client.call(image_route + "/rows?table=node")
    expect(rows["rows"][0]["key"] == [["t", "north"], ["i", "1"]], "主键编码不正确")
    expect(rows["rows"][0]["values"][3] == ["b", "ABQ="], "BLOB 上屏丢失")
    expect(client.call(image_route + "/rows?table=none", status=404)["code"] == "unknown_table", "未知表未拒绝")
    page = client.call(f"/images/{base['id']}/rows?" + urlencode({"table": "node"}))
    expect(b"ABQ=" in page, "行详情未渲染")
    if done("rows", "HTML 与 JSON 保留复合键和 BLOB"):
        return
    alternate = client.upload("quartz extra index", mismatched_schema())
    sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
    error = client.call("/api/plans", dict(sources, right=alternate["id"]), 409)
    expect(error["code"] == "schema_mismatch", "结构不一致未拒绝")
    plan = client.call("/api/plans", sources, 201)
    expect(plan["revision"] == 0 and len(plan["items"]) == 3, "三方计划内容不符")
    if done("plan", "两项单边改动、一项冲突，模式不一致拒绝"):
        return
    route = f"/api/plans/{plan['id']}"
    page = client.call(f"/plans/{plan['id']}")
    expect("主键行差异".encode() in page and "1 项待裁决".encode() in page, "计划页未展示冲突")
    expect(client.call(route)["unresolved"] == 1, "未决数不符")
    expect(client.call(route + "/rehearse", {"revision": 0}, 409)["code"] == "unresolved", "未决重演未拦住")
    review_paging(client)
    if done("review", "待决数与可重演条件一致，分批翻页保留版本和继续位置"):
        return
    conflict = next(item for item in plan["items"] if not item["automatic"])
    decision = {"item": conflict["id"], "choice": "right", "revision": 0}
    expect(client.call(route + "/decisions", decision)["revision"] == 1, "首次裁决没有推进版本")
    expect(client.call(route + "/decisions", decision, 409)["code"] == "stale_revision", "过期裁决未拒绝")
    expect(client.call(route + "/decisions", dict(decision, revision=1))["revision"] == 1, "重复裁决修改了版本")
    if done("decide", "过期写入拒绝，同选择不推进版本"):
        return
    accepted = client.call(route + "/rehearse", {"revision": 1})
    expect(accepted["status"] == "passed" and accepted["changed"] == 3, "正常候选未通过")
    stress = {"base": base["id"], "left": client.upload("stress-left")["id"],
              "right": client.upload("stress-right")["id"]}
    failed_plan = client.call("/api/plans", stress, 201)
    stress_route = f"/api/plans/{failed_plan['id']}"
    issue = client.call(stress_route + "/rehearse", {"revision": 0})
    expect(issue["status"] == "blocked" and issue["problem"]["code"] == "constraint", "合成唯一性问题未捕获")
    expect(client.call(stress_route + "/download", status=404)["code"] == "unpublished", "失败候选可下载")
    if done("rehearse", "有效候选通过，两个合法输入的唯一键冲突产生诊断"):
        return
    expect(client.call(route + "/rehearse", {"revision": 1}) == accepted, "同版重演没有复用证据")
    client.call(route + "/decisions", dict(decision, revision=1, choice="base"))
    history = client.call(route + "/attempts")
    expect(history == [accepted], "历史记录改变")
    expect(client.call(route + "/seal", {"revision": 2}, 409)["code"] == "rehearsal_required", "旧记录可封存")
    if done("attempts", "旧记录保留但不能封存新版本"):
        return
    current = client.call(route + "/rehearse", {"revision": 2})
    certificate = client.call(route + "/seal", {"revision": 2})
    expect(client.call(route + "/seal", {"revision": 2}) == certificate, "重复封存结果改变")
    expect(client.call(route + "/decisions", dict(decision, revision=2), 409)["code"] == "closed_plan", "封存可编辑")
    if done("seal", "原子封存当前候选，重复封存稳定"):
        return
    proof = client.call(route + "/certificate")
    expect(proof["sources"] == sources and proof["candidate_digest"] == current["candidate_digest"] and
           proof["schema"] == catalog["signature"] and proof["revision"] == 2, "封存凭证来源错误")
    expect(client.call(stress_route + "/certificate", status=404)["code"] == "unpublished", "失败候选可颁证")
    if done("certificate", "来源角色、模式、候选和版本精确绑定"):
        return
    candidate = client.call(route + "/download")
    expect(hashlib.sha256(candidate).hexdigest() == proof["candidate_digest"], "下载文件与凭证不匹配")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], "结果存在外键违规")
        expect(db.execute("SELECT note FROM node WHERE bench='north' AND slot=1").fetchone() == ("祖本节点",),
               "下载数据库中的裁决结果不符")
    finally:
        db.close()
    if done("download", "下载摘要校验一致，SQLite 可独立打开和查询"):
        return
    client.call(route + "/archive", {"revision": 2})
    expect(client.call(route)["state"] == "archived", "归档状态丢失")
    expect(client.call(route + "/rehearse", {"revision": 2}, 409)["code"] == "closed_plan", "归档计划可重演")
    expect(client.call(route + "/download") == candidate and client.call(route + "/certificate") == proof,
           "归档后成果变化")
    done("archive", "停止写入，既有成果和凭证继续可提取")


def main():
    parser = argparse.ArgumentParser(description="验证 Pagefold 生产工作流")
    parser.add_argument("workflow", choices=("all", *WORKFLOWS))
    with tempfile.TemporaryDirectory(prefix="pagefold-walkthrough-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(Client(server.server_port), parser.parse_args().workflow)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
