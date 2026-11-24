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

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "packets", "rehearse", "attempts",
             "seal", "certificate", "download", "archive")


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
    if done("review", "待决数与可重演条件一致"):
        return
    conflict = next(item for item in plan["items"] if not item["automatic"])
    decision = {"item": conflict["id"], "choice": "right", "revision": 0}
    expect(client.call(route + "/decisions", decision)["revision"] == 1, "首次裁决没有推进版本")
    expect(client.call(route + "/decisions", decision, 409)["code"] == "stale_revision", "过期裁决未拒绝")
    expect(client.call(route + "/decisions", dict(decision, revision=1))["revision"] == 1, "重复裁决修改了版本")
    if done("decide", "过期写入拒绝，同选择不推进版本"):
        return
    packet_check(client, done)
    if done("packets", "两份重叠分发包各自只含指定行，并回写可追踪的选择"):
        return
    continue_checks(client, sources, catalog, route, decision, done)


def packet_check(client, done):
    base, left, right = (client.upload(name) for name in ("packet-base", "packet-left", "packet-right"))
    plan = client.call("/api/plans", {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)
    route = f"/api/plans/{plan['id']}"
    rows = {(item["key"][0][1], int(item["key"][1][1])): item for item in plan["items"]}
    expect(len(rows) == 5 and plan["unresolved"] == 4, "分发包样本的冲突数不符")
    expect(client.call(route + "/packets", {"revision": 0, "recipient": "甲", "items": ["0" * 64]}, 409)["code"]
           == "not_conflict", "非冲突行被带入分发包")
    client.call(route + "/decisions", {"revision": 0, "item": rows[("north", 2)]["id"], "choice": "left"})
    first = client.call(route + "/packets", {"revision": 1, "recipient": "甲",
                                             "items": [rows[("north", 1)]["id"], rows[("north", 2)]["id"]]})
    second = client.call(route + "/packets", {"revision": 1, "recipient": "乙",
                                              "items": [rows[("north", 2)]["id"], rows[("south", 1)]["id"]]})
    packet_ids = {first["id"], second["id"]}
    expect([item["key"] for item in first["items"]] == [[["t", "north"], ["i", "1"]], [["t", "north"], ["i", "2"]]],
           "分发包行集合或顺序不符")
    expect({item["id"] for item in second["items"]} == {rows[("north", 2)]["id"], rows[("south", 1)]["id"]},
           "第二包行集合不符")
    exported = client.call(f"/api/packets/{first['id']}/export")
    leaked = {rows[("south", 1)]["id"], rows[("south", 2)]["id"], rows[("west", 1)]["id"]}
    expect({item["id"] for item in exported["items"]} == {item["id"] for item in first["items"]},
           "导出包包含未分发到该包的行")
    expect(not leaked & {item["id"] for item in exported["items"]}, "未分发的行随包外泄")
    expect(set(exported["tables"]) == {"node"} and "south" not in json.dumps(exported),
           "导出包带出了包外表或未分发值")
    expect(exported["items"][1]["choice"] == "left" and exported["items"][0]["choice"] is None,
           "包内选择不是发出时的值")
    expect(client.call(f"/api/packets/{second['id']}/return", {
        "digest": second["digest"], "selections": {rows[("south", 2)]["id"]: "left"}}, 409)["code"]
        == "out_of_packet", "包外行选择未被拒绝")
    due = client.call(route + "/decisions", {"revision": 1, "item": rows[("south", 2)]["id"], "choice": "base"})
    expect(due["revision"] == 2, "分发期间的其他裁决未推进版本")
    client.call(f"/api/packets/{first['id']}/return", {"digest": first["digest"], "selections": {
        rows[("north", 1)]["id"]: "right", rows[("north", 2)]["id"]: ["clear", "left"]}, "note": "甲已复核"})
    merged = client.call(f"/api/packets/{first['id']}/merge", {"revision": 2})
    expect([entry["key"] for entry in merged["applied"]] == [[["t", "north"], ["i", "1"]]],
           "包内新选择未安全并入")
    expect([entry["key"] for entry in merged["unchanged"]] == [[["t", "north"], ["i", "2"]]]
           and merged["unchanged"][0]["note"] == "cleared_then_restored", "清空后选回原值未被识别")
    expect(merged["plan_revision_before"] == 2 and merged["plan_revision_after"] == 3,
           "合入未基于最新版序推进")
    client.call(f"/api/packets/{second['id']}/return", {"digest": second["digest"], "selections": {
        rows[("north", 2)]["id"]: "right", rows[("south", 1)]["id"]: "left"}})
    current = client.call(route + "/decisions", {"revision": 3, "item": rows[("south", 1)]["id"], "choice": "right"})
    expect(current["revision"] == 4, "无关行裁决未推进版本")
    conflict = client.call(f"/api/packets/{second['id']}/merge", {"revision": 4})
    expect(conflict["state"] == "returned" and [entry["key"] for entry in conflict["pending"]]
           == [[["t", "south"], ["i", "1"]]], "被他人改过选择的行未被拦下")
    expect([entry["key"] for entry in conflict["applied"]] == [[["t", "north"], ["i", "2"]]],
           "与包无关的选择未合入")
    expect(conflict["plan_revision_before"] == 4 and conflict["plan_revision_after"] == 5,
           "部分合入未推进版本")
    expect(conflict["applied"][0]["previous"] == "left" and conflict["applied"][0]["choice"] == "right",
           "合入没有记录前后选择")
    expect(client.call(f"/api/packets/{second['id']}/merge", {"revision": 4}, 409)["code"] == "stale_revision",
           "部分合入后的旧版序未被拒绝")
    expect(client.call(f"/api/packets/{second['id']}/merge", {"revision": 2}, 409)["code"] == "stale_revision",
           "过期版序未被拒绝")
    client.call(f"/api/packets/{second['id']}/return", {"digest": second["digest"], "selections": {
        rows[("north", 2)]["id"]: "right", rows[("south", 1)]["id"]: "left"}})
    settled = client.call(f"/api/packets/{second['id']}/merge", {"revision": 5})
    expect(settled["state"] == "merged" and settled["plan_revision_before"] == 5
           and settled["plan_revision_after"] == 6 and
           [entry["key"] for entry in settled["applied"]] == [[["t", "south"], ["i", "1"]]]
           and settled["applied"][0]["previous"] == "right" and settled["applied"][0]["choice"] == "left",
           "本人在同一包内确认后未合入")
    expect(client.call(route)["unresolved"] == 0 and client.call(route)["revision"] == 6, "合入后计划仍有未决")
    expect(client.call(route + "/rehearse", {"revision": 6})["status"] == "passed", "合入后候选未通过")
    log = client.call(route + "/decisions")
    expect([(entry["revision"], entry["origin"]) for entry in log] == [
        (1, "direct"), (2, "direct"), (3, "packet"), (4, "direct"), (5, "packet"), (6, "packet")],
        "裁决回写来源不可追踪")
    expect([entry["actor"] for entry in log if entry["origin"] == "packet"] == ["甲", "乙", "乙"],
           "分发包来源人不可追踪")
    page = client.call(f"/plans/{plan['id']}")
    expect(all(packet_id[:8].encode() in page for packet_id in packet_ids), "计划页未列出分发包")


def continue_checks(client, sources, catalog, route, decision, done):
    accepted = client.call(route + "/rehearse", {"revision": 1})
    expect(accepted["status"] == "passed" and accepted["changed"] == 3, "正常候选未通过")
    stress = {"base": sources["base"], "left": client.upload("stress-left")["id"],
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
