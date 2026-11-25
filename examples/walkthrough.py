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

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts", "seal",
             "certificate", "download", "archive")


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
    ledger = image_route + "/rows?" + urlencode({"table": "ledger"})
    first = client.call(ledger)
    expect(first["total"] > 100 and len(first["rows"]) == 50, "示例表不足以跨页")
    expect(first["rows"][0]["key"] == [["t", "Alpha"], ["b", ""]], "空 BLOB 键次序不正确")
    expect(first["rows"][1]["key"] == [["t", "Alpha"], ["i", "2"]], "整数与等值实数未共用行身份")
    expect(first["rows"][3]["key"] == [["t", "Alpha"], ["t", ""]], "空文本键次序不正确")
    expect(first["rows"][5]["key"] == [["t", "alpha"], ["b", ""]], "大小写文本键未按 BINARY 次序")
    located = client.call(ledger + "&" + urlencode({"key": '{"site":["t","north"],"marker":["i","8"]}'}))
    expect(located["offset"] == 100 and located["placement"]["found"], "类型化键未跳到目标页")
    expect(any(row["key"] == [["t", "north"], ["i", "8"]] for row in located["rows"]), "目标行不在窗口内")
    absent_key = '{"site":["t","north"],"marker":["i","7"]}'
    absent = client.call(ledger + "&" + urlencode({"key": absent_key}))
    expect(not absent["placement"]["found"] and absent["placement"]["position"] > 0, "缺席键未给出插入位")
    expect(absent["placement"]["previous"]["key"] == [["t", "north"], ["i", "69"]], "缺席键的前邻不稳")
    expect(absent["placement"]["next"]["key"] == [["t", "north"], ["i", "70"]], "缺席键的后邻不稳")
    expect(absent["offset"] == (absent["placement"]["next"]["position"] // 50) * 50, "缺席键没有稳定页")
    banner = client.call(f"/images/{base['id']}/rows?" + urlencode({"table": "ledger", "key": absent_key}))
    expect("不在本表".encode() in banner and "接近的前一行".encode() in banner, "缺席位置未渲染")
    legacy = client.call(ledger + "&offset=50")
    expect(legacy["offset"] == 50 and legacy["placement"] is None and
           legacy["rows"][0]["position"] == 50, "旧 offset 链接含义改变")
    expect(client.call(ledger + "&" + urlencode({"key": "north"}), status=400)["code"] == "key_json",
           "裸标签被当作键")
    expect(client.call(ledger + "&" + urlencode({"key": '{"site":["t","north"]}'}), status=400)["code"] == "key_shape",
           "残缺复合键未拒绝")
    expect(client.call(ledger + "&" + urlencode({"key": '{"site":["t","north"],"marker":["n"]}'}),
                       status=400)["code"] == "key_atom", "NULL 键原子未拒绝")
    if done("rows", "类型化复合键定位、缺席邻位、旧 offset 保留"):
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
    expect(b'class="locate-links"' in page, "计划项没有来源定位入口")
    expect(b"absent" in page, "缺席角色未标注")
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
