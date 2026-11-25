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
             "certificate", "download", "recover", "archive")


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


def fk_image(flavor):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(image("quartz"))
        db.execute("PRAGMA foreign_keys=ON")
        if flavor == "left":
            db.execute("DELETE FROM binding WHERE id=1")
            db.execute("DELETE FROM node WHERE bench='north' AND slot=1")
            db.execute("UPDATE node SET note='左支备注' WHERE bench='south' AND slot=1")
        else:
            db.execute("INSERT INTO binding VALUES(2,'north',1)")
            db.execute("UPDATE node SET note='右支备注' WHERE bench='south' AND slot=1")
        db.commit()
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
    stress_now = client.call(stress_route)
    target = next(item for item in stress_now["items"] if item["key"] == [["t", "stress-right"]])
    expect(target["automatic"] == "right" and target["escalation"] is None, "压力样例应为明确的自动项")
    expect(client.call(stress_route + "/escalations", {"item": target["id"], "reason": "  ", "revision": 0}, 400)
           ["code"] == "reason", "空理由未拒绝")
    expect(client.call(stress_route + "/escalations", {"item": target["id"], "reason": "理由", "revision": 9}, 409)
           ["code"] == "stale_revision", "过期转复核未拒绝")
    moved = client.call(stress_route + "/escalations",
                        {"item": target["id"], "reason": "两侧唯一标签撞名，保留左支行", "revision": 0})
    expect(moved["revision"] == 1, "转复核未推进版本")
    pending = client.call(stress_route)
    expect(pending["unresolved"] == 1, "待复核项未计入未决")
    record = next(item for item in pending["items"] if item["id"] == target["id"])["escalation"]
    expect(record["reason"] == "两侧唯一标签撞名，保留左支行" and record["automatic"] == "right", "复核理由未保存")
    expect(client.call(stress_route + "/rehearse", {"revision": 1}, 409)["code"] == "unresolved", "待复核项未拦住重演")
    client.call(stress_route + "/decisions", {"item": target["id"], "choice": "base", "revision": 1})
    recovered = client.call(stress_route + "/rehearse", {"revision": 2})
    expect(recovered["status"] == "passed" and recovered["changed"] == 1, "唯一键压力样例未恢复")
    stress_proof = client.call(stress_route + "/seal", {"revision": 2})
    expect("escalation_digest" in stress_proof, "凭证缺少复核摘要")
    history = client.call(stress_route + "/attempts")
    expect([a["status"] for a in history] == ["passed", "blocked"], "压力样例受阻历史未保留")
    merged = client.call(stress_route + "/download")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(merged)
        expect(db.execute("SELECT label FROM bench WHERE code='stress-left'").fetchone() == ("共享键值",),
               "左支唯一行未保留")
        expect(db.execute("SELECT 1 FROM bench WHERE code='stress-right'").fetchone() is None, "右支行应被拒绝")
    finally:
        db.close()
    fk_sources = {"base": base["id"], "left": client.upload("fk-left", fk_image("left"))["id"],
                  "right": client.upload("fk-right", fk_image("right"))["id"]}
    fk_plan = client.call("/api/plans", fk_sources, 201)
    fk_route = f"/api/plans/{fk_plan['id']}"
    expect(fk_plan["unresolved"] == 1 and len(fk_plan["items"]) == 4, "外键样例计划内容不符")
    clash = next(item for item in fk_plan["items"] if not item["automatic"])
    binding = next(item for item in fk_plan["items"] if item["table"] == "binding" and item["key"] == [["i", "1"]])
    node = next(item for item in fk_plan["items"] if item["table"] == "node" and item["key"][0] == ["t", "north"])
    expect(client.call(fk_route + "/escalations", {"item": binding["id"], "reason": "过早", "revision": 0}, 409)
           ["code"] == "not_blocked", "未受阻计划允许转复核")
    client.call(fk_route + "/decisions", {"item": clash["id"], "choice": "left", "revision": 0})
    issue = client.call(fk_route + "/rehearse", {"revision": 1})
    expect(issue["status"] == "blocked" and issue["problem"]["code"] == "foreign_key", "外键受阻未捕获")
    expect(client.call(fk_route + "/escalations", {"item": clash["id"], "reason": "冲突项", "revision": 1}, 409)
           ["code"] == "not_automatic", "非自动项允许转复核")
    client.call(fk_route + "/escalations", {"item": binding["id"], "reason": "绑定删除牵涉外键，先保留该行", "revision": 1})
    client.call(fk_route + "/decisions", {"item": binding["id"], "choice": "right", "revision": 2})
    again = client.call(fk_route + "/rehearse", {"revision": 3})
    expect(again["status"] == "blocked" and again["problem"]["code"] == "foreign_key", "保留单行后应仍受阻")
    expect(client.call(fk_route + "/escalations", {"item": binding["id"], "reason": "重复", "revision": 3}, 409)
           ["code"] == "already_escalated", "重复转复核未拒绝")
    client.call(fk_route + "/escalations", {"item": node["id"], "reason": "保留父行使两条绑定外键同时成立", "revision": 3})
    client.call(fk_route + "/decisions", {"item": node["id"], "choice": "base", "revision": 4})
    fixed = client.call(fk_route + "/rehearse", {"revision": 5})
    expect(fixed["status"] == "passed" and fixed["changed"] == 2, "外键样例未恢复")
    fk_proof = client.call(fk_route + "/seal", {"revision": 5})
    expect("escalation_digest" in fk_proof and fk_proof["escalation_digest"] != stress_proof["escalation_digest"],
           "复核摘要未区分计划")
    history = client.call(fk_route + "/attempts")
    expect([a["status"] for a in history] == ["passed", "blocked", "blocked"], "外键受阻历史未保留")
    expect([a["revision"] for a in history] == [5, 3, 1], "历史版本顺序不符")
    page = client.call(f"/plans/{fk_plan['id']}")
    expect("保留父行使两条绑定外键同时成立".encode() in page and "人工 · ".encode() in page, "计划页未展示复核理由")
    merged = client.call(fk_route + "/download")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(merged)
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], "结果存在外键违规")
        expect(sorted(db.execute("SELECT id,bench,slot FROM binding")) == [(1, "north", 1), (2, "north", 1)],
               "绑定行不符")
        expect(db.execute("SELECT note FROM node WHERE bench='north' AND slot=1").fetchone() == ("祖本节点",),
               "父行未保留")
        expect(db.execute("SELECT note FROM node WHERE bench='south' AND slot=1").fetchone() == ("左支备注",),
               "冲突裁决未生效")
    finally:
        db.close()
    if done("recover", "唯一键与外键受阻计划经转复核恢复，受阻历史与复核理由保留"):
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
        legacy = Path(root) / "legacy-state.sqlite"
        db = sqlite3.connect(legacy)
        try:
            db.executescript("""CREATE TABLE plans(id TEXT PRIMARY KEY, base TEXT NOT NULL,
                left_id TEXT NOT NULL, right_id TEXT NOT NULL, items TEXT NOT NULL,
                decisions TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'open')""")
        finally:
            db.close()
        with Store(legacy).connect() as check:
            columns = {row[1] for row in check.execute("PRAGMA table_info(plans)")}
        expect("escalations" in columns, "旧状态库未迁移出 escalations 列")
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
