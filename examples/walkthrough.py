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
             "certificate", "download", "archive", "reclaim-preview", "reclaim-confirm")


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
    if done("archive", "停止写入，既有成果和凭证继续可提取"):
        return
    reclaim(client, goal, done, base)


def variant(code):
    """A quartz clone that only differs by one extra bench row (same schema)."""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(image("quartz"))
        db.execute("INSERT INTO bench VALUES(?,?)", (code, "变体 " + code))
        db.commit()
        return db.serialize()
    finally:
        db.close()


def reclaim(client, goal, done, base):
    """Preview and confirm reclaim across shared, sealed, archived and orphan sources."""
    ancestor = base["id"]  # quartz, also referenced by the earlier plans
    left1, right1 = client.upload("left1", variant("l1"))["id"], client.upload("right1", variant("r1"))["id"]
    left2, right2 = client.upload("left2", variant("l2"))["id"], client.upload("right2", variant("r2"))["id"]
    trunk, branch = client.upload("trunk", variant("t1"))["id"], client.upload("branch", variant("b1"))["id"]
    orphan = client.upload("orphan", variant("o1"))["id"]
    spare = client.upload("spare", variant("s1"))["id"]
    expect(client.upload("spare", variant("s1"))["id"] == spare, "同内容镜像未复用为同一摘要")

    # 1) Open plan: its sources must survive because it can still be rehearsed.
    open_plan = client.call("/api/plans", {"base": ancestor, "left": left1, "right": right1}, 201)
    # 2) Sealed plan: 封存结果 keeps its sources.
    sealed_plan = client.call("/api/plans", {"base": ancestor, "left": left2, "right": right2}, 201)
    client.call(f"/api/plans/{sealed_plan['id']}/rehearse", {"revision": 0})
    client.call(f"/api/plans/{sealed_plan['id']}/seal", {"revision": 0})
    # 3) Sealed then archived: still downloadable, sources kept for certificate review.
    reviewed = client.call("/api/plans", {"base": trunk, "left": branch, "right": ancestor}, 201)
    client.call(f"/api/plans/{reviewed['id']}/rehearse", {"revision": 0})
    client.call(f"/api/plans/{reviewed['id']}/seal", {"revision": 0})
    client.call(f"/api/plans/{reviewed['id']}/archive", {"revision": 0})
    reviewed_release = client.call(f"/api/plans/{reviewed['id']}/download")
    # 4) Archived without sealing: history stays, but its sources may be freed.
    archived = client.call("/api/plans", {"base": orphan, "left": client.upload("ab", variant("a1"))["id"],
                                          "right": client.upload("ar", variant("a2"))["id"]}, 201)
    client.call(f"/api/plans/{archived['id']}/archive", {"revision": 0})

    preview = client.call("/api/reclaims")
    expect(preview["format"] == "pagefold-reclaim-v1", "回收预审格式不符")
    reclaimable = {entry["id"]: entry for entry in preview["reclaimable"]}
    retained = {entry["id"]: entry for entry in preview["retained"]}
    expect(ancestor in retained, "共享祖本未被保留")
    expect(set(retained[ancestor]["reasons"]) ==
           {"open_plan", "sealed_result", "certificate_review"}, "共享祖本未记录三种保留理由")
    roles = {ref["role"] for ref in retained[ancestor]["references"]}
    expect({"base", "right"} <= roles, "同一文件在不同计划中的角色未分别记录")
    expect(left1 in retained and left2 in retained and trunk in retained, "开放/封存/复核来源未保留")
    expect(orphan in reclaimable, "归档未封存计划的来源未列为可回收")
    expect(preview["freed_bytes"] == sum(entry["size"] for entry in preview["reclaimable"]),
           "释放体积不等于去重后可回收镜像之和")
    expect(len(preview["reclaimable"]) == len(reclaimable), "回收清单存在重复镜像")
    expect(sum(entry["id"] == spare for entry in preview["reclaimable"]) == 1, "同内容镜像被重复计入")
    page = client.call("/reclaims")
    expect("原始镜像回收".encode() in page and "确认回收".encode() in page, "回收预审页未渲染")
    if done("reclaim-preview", f"保留 {len(retained)} 份（开放/封存/复核），可回收 {len(reclaimable)} 份，"
                              f"同内容仅计一次，预计释放 {preview['freed_bytes']:,} B"):
        return

    # A reference added after previewing must invalidate the report and survive.
    client.call("/api/plans", {"base": spare, "left": client.upload("l4", variant("l4"))["id"],
                                      "right": client.upload("r4", variant("r4"))["id"]}, 201)
    stale = client.call("/api/reclaims", {"digest": preview["digest"]}, 409)
    expect(stale["code"] == "stale_report", "新增引用后仍执行旧预审")
    expect(client.call(f"/api/images/{spare}/rows?table=bench")["total"] > 0, "过期预审删除了新引用的来源")
    refreshed = client.call("/api/reclaims")
    expect(refreshed["digest"] != preview["digest"], "预审摘要未反映新增引用")
    expect(spare not in {entry["id"] for entry in refreshed["reclaimable"]}, "新引用来源仍可回收")

    execution = client.call("/api/reclaims", {"digest": refreshed["digest"]})
    expect(execution["format"] == "pagefold-reclaim-execution-v1", "回收执行格式不符")
    expect(execution["freed_bytes"] == refreshed["freed_bytes"], "实际释放与预审不一致")
    expect({entry["id"] for entry in execution["reclaimed"]} ==
           {entry["id"] for entry in refreshed["reclaimable"]}, "执行清单与预审不一致")
    again = client.call("/api/reclaims", {"digest": refreshed["digest"]})
    expect(again["repeated"] and again["freed_bytes"] == execution["freed_bytes"], "重复执行结果改变")
    expect(client.call("/api/reclaims/" + refreshed["digest"])["freed_bytes"] == execution["freed_bytes"],
           "回收记录无法按摘要查询")
    expect(client.call("/api/reclaims/history")[0]["digest"] == refreshed["digest"], "回收历史缺失")

    # Freed sources lose only their rows; catalog, ids and sizes survive.
    expect(client.call(f"/api/images/{orphan}")["reclaimed"], "回收后镜像未标记")
    expect(client.call(f"/api/images/{orphan}/rows?table=bench", status=410)["code"] == "image_reclaimed",
           "回收后的行仍可读取")
    expect(client.call(f"/api/images/{orphan}")["catalog"]["signature"], "回收后目录丢失")

    # Retained reference, frozen release, and archived history stay readable.
    expect(client.call(f"/api/images/{ancestor}/rows?table=node")["rows"][0]["values"][3] == ["b", "ABQ="],
           "保留来源的行数据不可读")
    expect(client.call(f"/api/plans/{open_plan['id']}")["state"] == "open", "开放计划状态改变")
    expect(client.call(f"/api/plans/{sealed_plan['id']}/certificate")["sources"]["base"] == ancestor,
           "封存凭证丢失来源")
    expect(client.call(f"/api/plans/{reviewed['id']}/download") == reviewed_release, "归档成果下载改变")
    expect(client.call(f"/api/images/{ancestor}/rows?table=bench")["total"] > 0, "复核来源不可读")
    expect(client.call(f"/api/plans/{archived['id']}")["items"], "归档未封存计划的历史丢失")
    expect("合流计划".encode() in client.call(f"/plans/{archived['id']}"), "归档计划页无法渲染")
    blocked = client.call("/api/plans", {"base": orphan, "left": left1, "right": right1}, 410)
    expect(blocked["code"] == "image_reclaimed", "已回收来源仍可建立新计划")
    done("reclaim-confirm", f"过期预审被拒并保留新引用；执行释放 {execution['freed_bytes']:,} B，"
                            f"{len(execution['reclaimed'])} 份去重镜像；保留来源、封存成果与归档历史仍可读")


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
