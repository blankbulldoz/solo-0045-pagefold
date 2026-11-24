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

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts",
             "compare", "seal", "certificate", "download", "archive")


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


def collision_image(flavor):
    """同一计划内先通过、后因唯一键选择受阻所需的三份镜像。

    g1 是双边冲突：左支改成 left-label，右支改成 dup；左支单边插入的
    g2 也使用 dup。选左支全部唯一，选右支则与 g2 撞唯一约束。
    """
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE gadget(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID")
        db.execute("INSERT INTO gadget VALUES('g1','one')")
        if flavor == "collision-left":
            db.execute("UPDATE gadget SET label='left-label' WHERE code='g1'")
            db.execute("INSERT INTO gadget VALUES('g2','dup')")
        elif flavor == "collision-right":
            db.execute("UPDATE gadget SET label='dup' WHERE code='g1'")
            db.execute("INSERT INTO gadget VALUES('g3','right-only')")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def run(client, goal, state_path=None):
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
    pair = client.call(route + "/compare?a=1&b=2")
    expect(pair["state"] == "candidate_diff" and pair["same_candidate"] is False, "成功/成功比较状态错误")
    north = next(e for e in pair["changes"] if e["table"] == "node" and e["proven_change"] is True)
    expect(north["choice_changed"] is True, "候选实证差异未关联选择变化")
    expect(north["a"]["choice"] == "right" and north["b"]["choice"] == "base", "未关联两版当时的选择")
    expect(north["a"]["row_evidence"] == "proven" and north["b"]["row_evidence"] == "proven",
           "成功侧行未由候选证明")
    expect(client.call(route + "/attempts/compare?a=1&b=2")["state"] == pair["state"],
           "attempts/compare 别名入口不一致")
    same = client.call(route + "/compare?a=1&b=1")
    expect(same["same_candidate"] is True and same["state"] == "same_candidate", "同版比较未识别同一候选")
    verified = client.call(route + "/compare?a=1&b=2&verify=1")
    expect(all(side["reproduction"].get("reproduced") is True for side in verified["sides"]),
           "可重现来源的离线复核未通过")
    compare_page = client.call(f"/plans/{plan['id']}/compare?a=1&b=2&verify=1")
    for token in ("两版重演比较", "已证明", "候选实证", "可重现的来源", "离线重放得到相同候选摘要"):
        expect(token.encode() in compare_page, f"成功/成功比较页缺少 {token}")
    plan_page_html = client.call(f"/plans/{plan['id']}")
    expect(b"history-compare" in plan_page_html and b"/compare?" in plan_page_html, "计划页缺少比较入口")
    expect(client.call(route + "/compare", status=400)["code"] == "compare_revisions", "缺少比较版本未拒绝")
    expect(client.call(route + "/compare?a=1&b=99", status=404)["code"] == "unknown_attempt",
           "未知重演版本未拒绝")
    csources = {"base": client.upload("collision-base", collision_image("base"))["id"],
                "left": client.upload("collision-left", collision_image("collision-left"))["id"],
                "right": client.upload("collision-right", collision_image("collision-right"))["id"]}
    cplan = client.call("/api/plans", csources, 201)
    grow = next(item for item in cplan["items"] if not item["automatic"])
    croute = f"/api/plans/{cplan['id']}"
    client.call(croute + "/decisions", {"item": grow["id"], "choice": "left", "revision": 0})
    first = client.call(croute + "/rehearse", {"revision": 1})
    expect(first["status"] == "passed", "成功/阻断样本的第一版未通过")
    client.call(croute + "/decisions", {"item": grow["id"], "choice": "right", "revision": 1})
    blocked = client.call(croute + "/rehearse", {"revision": 2})
    expect(blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint",
           "第二版未被唯一约束阻断")
    mixed = client.call(croute + "/compare?a=1&b=2")
    expect(mixed["state"] == "passed_blocked", "成功/阻断比较状态错误")
    grow_row = next(e for e in mixed["rows"] if e["item_id"] == grow["id"])
    expect(grow_row["a"]["choice"] == "left" and grow_row["b"]["choice"] == "right", "阻断侧未关联当时选择")
    expect(grow_row["a"]["row_evidence"] == "proven" and grow_row["b"]["row_evidence"] == "inferred",
           "阻断侧不应把推断声称为证明")
    expect(any(e["blocker"] for e in mixed["rows"]), "未标记约束失败的操作行")
    mixed_verify = client.call(croute + "/compare?a=1&b=2&verify=1")
    expect(mixed_verify["b"]["reproduction"].get("reproduced") is True, "阻断点未离线重现")
    blocked_page = client.call(f"/plans/{cplan['id']}/compare?a=1&b=2&verify=1")
    for token in ("成功 / 阻断", "唯一约束", "待重演推断", "触发约束失败", "阻断点可重现"):
        expect(token.encode() in blocked_page, f"成功/阻断比较页缺少 {token}")
    if state_path is not None:
        legacy_db = sqlite3.connect(state_path)
        try:
            stored = legacy_db.execute(
                "SELECT revision,result FROM attempts WHERE plan_id=?", (cplan["id"],)).fetchall()
            for rev, text in stored:
                legacy = json.loads(text)
                legacy.pop("choices", None)
                legacy.pop("decision_digest", None)
                legacy_db.execute("UPDATE attempts SET result=? WHERE plan_id=? AND revision=?",
                                  (json.dumps(legacy, sort_keys=True, separators=(",", ":")),
                                   cplan["id"], rev))
            legacy_db.commit()
        finally:
            legacy_db.close()
        old_pair = client.call(croute + "/compare?a=1&b=2")
        old_row = next(e for e in old_pair["rows"] if e["item_id"] == grow["id"])
        expect(old_row["a"]["choice_evidence"] == "candidate", "旧通过记录未走候选反查来源")
        expect(old_row["b"]["choice_evidence"] == "current", "旧阻断记录未回退到当前选择推断")
        old_page = client.call(f"/plans/{cplan['id']}/compare?a=1&b=2")
        for token in ("候选反查来源", "当前版推断", "旧记录未留存选择"):
            expect(token.encode() in old_page, f"旧数据缺失选择的呈现缺少 {token}")
        client.call(croute + "/decisions", {"item": grow["id"], "choice": "clear", "revision": 2})
        missing_pair = client.call(croute + "/compare?a=1&b=2")
        missing_row = next(e for e in missing_pair["rows"] if e["item_id"] == grow["id"])
        expect(missing_row["b"]["choice_evidence"] == "missing"
               and missing_row["b"]["row_evidence"] == "unknown", "选择缺失未明确呈现")
        missing_page = client.call(f"/plans/{cplan['id']}/compare?a=1&b=2")
        expect("选择缺失".encode() in missing_page and "不能还原".encode() in missing_page,
               "缺失选择的页面标记不足")
    if done("compare", "成功/成功与成功/阻断可比较，候选实证、推断边界、可重现来源和旧数据缺失均有交代"):
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
        state_path = Path(root) / "state.sqlite"
        with make_server(Store(state_path), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(Client(server.server_port), parser.parse_args().workflow, state_path)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
