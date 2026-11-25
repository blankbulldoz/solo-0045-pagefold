import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

from examples.impact_check import verify

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts", "seal",
             "certificate", "download", "archive", "impact")


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

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


def run(client, goal, state_path):
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
    if done("review", "待决数与可重演条件一致"):
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
    if goal in ("impact", "all"):
        run_impact(client, state_path)
        print("impact ✓ 影响事实区分提议/选择/最终变动，旧凭证标明未知范围")


def run_impact(client, state_path):
    """一次封存覆盖：保留祖本、双边相同插入、删除后缺席三类易被混算的差异。"""
    base = client.upload("impact-base")
    left = client.upload("impact-left")
    right = client.upload("impact-right")
    sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
    plan = client.call("/api/plans", sources, 201)
    route = f"/api/plans/{plan['id']}"
    expect(plan["revision"] == 0 and len(plan["items"]) == 4, "影响场景的计划项数量不符")
    def tkey(code, kind):
        return [["t", code], ["t", kind]]

    by_key = {json.dumps(item["key"]): item for item in plan["items"]}
    conflict = by_key[json.dumps(tkey("beta", "one"))]
    expect(conflict["automatic"] is None, "beta 应为双边不同的人工冲突项")
    automatic = {json.dumps(item["key"]): item for item in plan["items"] if item["automatic"]}
    expect(set(automatic) == {json.dumps(tkey(*k)) for k in
                              (("alpha", "one"), ("gamma", "two"), ("delta", "one"))},
            "自动项集合不符")
    # 人工选择保留祖本：该项最终不产生任何写入
    decision = {"item": conflict["id"], "choice": "base", "revision": 0}
    expect(client.call(route + "/decisions", decision)["revision"] == 1, "冲突裁决未推进版本")
    rehearsal = client.call(route + "/rehearse", {"revision": 1})
    expect(rehearsal["status"] == "passed", "影响场景重演未通过")
    impact = rehearsal["impact"]
    expect(impact["format"] == "pagefold-impact-v1", "重演结果缺少影响事实")
    # 提议侧别：双边同插/同删 proposals 为双边，左支单边修改仅 left
    facts = {json.dumps(fact["key"]): fact for fact in impact["items"]}
    expect(facts[json.dumps(tkey("alpha", "one"))]["proposals"] == ["left", "right"]
           and facts[json.dumps(tkey("alpha", "one"))]["write"] == "insert", "双边同插分类错误")
    expect(facts[json.dumps(tkey("gamma", "two"))]["proposals"] == ["left", "right"]
           and facts[json.dumps(tkey("gamma", "two"))]["write"] == "delete", "双边同删分类错误")
    expect(facts[json.dumps(tkey("delta", "one"))]["proposals"] == ["left"]
           and facts[json.dumps(tkey("delta", "one"))]["write"] == "update", "单边修改分类错误")
    expect(facts[json.dumps(tkey("beta", "one"))]["origin"] == "manual"
           and facts[json.dumps(tkey("beta", "one"))]["choice"] == "base"
           and facts[json.dumps(tkey("beta", "one"))]["write"] == "none", "保留祖本应是人工且无写入")
    totals = impact["totals"]
    expect(totals["writes"] == {"insert": 1, "update": 1, "delete": 1, "none": 1}, "最终变动计数错误")
    expect(totals["origins"] == {"automatic": 3, "manual": 1}, "自动/人工计数错误")
    expect(totals["proposals"] == {"left": 1, "right": 0, "both": 3}, "左右提议计数错误")
    expect(impact["written_tables"] == ["ledger"], "写过表集合错误（journal、shelf 不应出现）")
    ledger = impact["tables"]["ledger"]
    expect(ledger["writes"] == {"insert": 1, "update": 1, "delete": 1, "none": 1}, "ledger 写入计数错误")
    expect(impact["tables"]["shelf"]["writes"] == {"insert": 0, "update": 0, "delete": 0, "none": 0}
           and impact["tables"]["shelf"]["rows"] == 2, "未写表被误记为受影响")
    expect(impact["tables"]["journal"]["rows"] == 2
           and impact["tables"]["journal"]["writes"]["none"] == 0, "无差异表不应有计划项")
    certificate = client.call(route + "/seal", {"revision": 1})
    expect(certificate["format"] == "pagefold-certificate-v2" and certificate["impact"] == impact
           and certificate["impact_digest"], "封存凭证未冻结影响事实")
    candidate = client.call(route + "/download")
    expect(hashlib.sha256(candidate).hexdigest() == certificate["candidate_digest"], "候选摘要不符")
    # 解包实际数据库，核对最终行状态
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        expect(db.execute("SELECT amount,note FROM ledger WHERE code='alpha'").fetchone()
               == (11, "双边同插"), "双边同插未落地")
        expect(db.execute("SELECT amount,note FROM ledger WHERE code='beta'").fetchone()
               == (10, "祖本内容"), "保留祖本未保持祖本行")
        expect(db.execute("SELECT 1 FROM ledger WHERE code='gamma'").fetchone() is None, "删除后键仍然存在")
        expect(db.execute("SELECT amount,note FROM ledger WHERE code='delta'").fetchone()
               == (31, "左支修改"), "左支单边修改未落地")
        expect(db.execute("SELECT count(*) FROM shelf").fetchone() == (2,), "未写入表行被改变")
    finally:
        db.close()
    # 用独立核验器重算并对账凭证
    catalog = client.call(f"/api/images/{base['id']}")["catalog"]
    errors, detail = verify(certificate, candidate, catalog, plan["items"])
    expect(not errors, f"影响事实核验失败：{errors}")
    expect(detail["rows_digest"] == impact["rows_digest"]
           and detail["counted_written"] == impact["written_tables"], "独立重算的摘要与凭证不符")
    # 旧版（v1、无 impact）凭证照常读取，并显式标明未知范围
    legacy = dict(certificate)
    legacy.pop("impact")
    legacy.pop("impact_digest")
    legacy["format"] = "pagefold-certificate-v1"
    with sqlite3.connect(state_path) as state:
        state.execute("UPDATE releases SET certificate=? WHERE plan_id=?",
                      (json.dumps(legacy, separators=(",", ":"), sort_keys=True), plan["id"]))
    seen = client.call(route + "/certificate")
    expect(seen["impact"]["scope"] == "unknown" and seen["impact_digest"] is None
           and seen["candidate_digest"] == certificate["candidate_digest"], "旧版凭证未标明未知范围")
    legacy_errors, _ = verify(seen, candidate, catalog, plan["items"])
    expect(legacy_errors and "未知" in "".join(legacy_errors), "旧版凭证应给出明确的未知结论")
    expect(client.call(route + "/download") == candidate, "标注未知范围后下载内容变化")


def main():
    parser = argparse.ArgumentParser(description="验证 Pagefold 生产工作流")
    parser.add_argument("workflow", choices=("all", *WORKFLOWS))
    with tempfile.TemporaryDirectory(prefix="pagefold-walkthrough-") as root:
        state_path = str(Path(root) / "state.sqlite")
        with make_server(Store(Path(state_path)), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(Client(server.server_port), parser.parse_args().workflow, state_path)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
