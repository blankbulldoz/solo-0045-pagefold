import argparse
import base64
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

WORKFLOWS = ("import", "group", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts", "seal",
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

    def group(self, entries, status=201, content_type="json"):
        if content_type == "json":
            payload_data = {role: {"label": label, "data": base64.b64encode(blob).decode()}
                            for role, (blob, label) in entries.items()}
            return self.call("/api/images", payload_data, status)
        return self.call("/api/images", _multipart_body(entries), status,
                         {"Content-Type": "multipart/form-data; boundary=----pagefold-walkthrough-boundary"})


def _multipart_body(entries):
    boundary = "----pagefold-walkthrough-boundary"
    chunks = []
    for role, (blob, label) in entries.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{role}"; filename="{label}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode())
        chunks.append(blob)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


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
    # ---- 带角色的三方组登记：原子回执、失败回滚、重试同一组 ----
    trio = {"base": (image("quartz"), "石英祖本"),
            "left": (image("cerulean"), "青蓝分支"),
            "right": (image("umber"), "赤褐分支")}
    before = {item["id"] for item in client.call("/api/images")}
    group = client.group(trio)
    expect(set(group["sources"]) == set(("base", "left", "right")), "组回执缺少角色映射")
    expect(group["images"]["left"]["label"] == "青蓝分支"
           and group["images"]["right"]["label"] == "赤褐分支", "组回执未保留角色标签")
    # 祖本此前已单份登记，内容寻址复用同一镜像与原始标签，不产生重复行。
    expect(group["sources"]["base"] == base["id"], "组登记未复用已登记镜像")
    expect(group["images"]["base"]["id"] == base["id"], "组回执未指回既有镜像")
    expect(len(client.call("/api/groups")) == 1, "组列表未出现新组")
    after = {item["id"] for item in client.call("/api/images")}
    expect(after - before == set(group["sources"].values()) - before, "首次组登记落库范围不符")
    # 完全相同的一批文件重发：复用全部镜像，只认同一组（模拟提交成功但页面未收到响应）。
    again = client.group(trio)
    expect(again["id"] == group["id"], "重发同批文件产生了第二组")
    expect({item["id"] for item in client.call("/api/images")} == after, "重发后出现额外镜像登记")
    # 混合有效/无效：一份损坏则整组拒绝，状态库不遗留任何部分登记。
    mixed = dict(trio, left=(b"not-a-sqlite-image", "损坏左支"))
    problem = client.group(mixed, 422)
    expect(problem["code"] in {"image_invalid", "image_header"} and "左支" in problem["detail"],
           "坏文件未标明角色")
    expect([g["id"] for g in client.call("/api/groups")] == [group["id"]], "失败提交残留了镜像组")
    expect({item["id"] for item in client.call("/api/images")} == after, "失败提交残留了部分镜像")
    # 结构合法但模式不一致的文件同样令整组失败。
    mismatch = dict(trio, right=(mismatched_schema(), "额外索引右支"))
    expect(client.group(mismatch, 409)["code"] == "schema_mismatch", "模式不一致未在组登记拦截")
    expect([g["id"] for g in client.call("/api/groups")] == [group["id"]], "模式失败残留了镜像组")
    # 重发同一批文件（JSON 入口）仍返回同一组。
    expect(client.group(trio)["id"] == group["id"], "重发同批文件未回到同一组")
    # multipart 表单入口与 JSON 入口给出同一组回执。
    expect(client.group(trio, content_type="form")["id"] == group["id"], "multipart 与 JSON 组登记不一致")
    detail = client.call(f"/api/groups/{group['id']}")
    expect(detail["sources"] == group["sources"] and len(detail["images"]) == 3, "组详情不完整")
    expect(client.call("/api/groups/missing", status=404)["code"] == "unknown_group", "未知组未拒绝")
    # 组可被计划直接承接，角色无需手工再选。
    plan = client.call("/api/plans", {"group": group["id"]}, 201)
    expect(plan["sources"] == group["sources"] and len(plan["items"]) == 3, "计划未承接镜像组")
    expect(client.call("/api/plans", {"group": group["id"], "base": next(iter(before))}, 201)["sources"]
           == group["sources"], "组承接被多余角色字段干扰")
    expect(client.call("/api/plans", {"group": "x" * 64}, status=400)["code"] == "token", "组标识未校验")
    # 已被其他计划用过的镜像仍可在新组复用：内容寻址，不锁定来源。
    reused = client.group({"base": (image("quartz"), "石英祖本"),
                           "left": (image("stress-left"), "受压左支"),
                           "right": (image("stress-right"), "受压右支")})
    expect(reused["sources"]["base"] == group["sources"]["base"], "已用镜像在新组未复用")
    expect(len(client.call("/api/groups")) == 2, "复用镜像未能登记第二组")
    # 缺角色、重复角色和错误媒体类型都被拒绝。
    expect(client.group({k: v for k, v in trio.items() if k != "right"}, 400)["code"] == "group_role",
           "缺少角色未拒绝")
    expect(client.call("/api/images", b"{}", 415, {"Content-Type": "text/plain"})["code"] == "media_type",
           "错误媒体类型未拒绝")
    if done("group", "整组原子回执、坏文件回滚、重发同组、计划承接与已用镜像复用"):
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
