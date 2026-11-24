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
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from pagefold.codec import fingerprint
from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts", "seal",
             "certificate", "download", "archive", "manual")


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

    def post_form(self, route, fields):
        """提交网页表单且不自动跟随 303，返回 (status, body)。"""
        request = Request(self.origin + route, data=urlencode(fields).encode(), method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        try:
            with build_opener(NoRedirect).open(request, timeout=10) as result:
                return result.status, result.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()


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
    done("archive", "停止写入，既有成果和凭证继续可提取")


def manual_images():
    """构造覆盖整型/文本/BLOB/NULL/空文本与唯一约束的三份镜像。"""
    schema = """
    CREATE TABLE tile(
      slot INTEGER PRIMARY KEY,
      code TEXT NOT NULL UNIQUE,
      title TEXT, note TEXT, payload BLOB, ratio REAL CHECK(ratio >= 0)
    );
    """
    base_rows = [
        (1, "alpha", "阿尔法", "祖本备注", b"\x00\x14", 1.5),
        (2, "beta", "贝塔", "", b"", 3.0),
        (3, "gamma", "伽马", None, None, 0.5),
    ]

    def build(flavor):
        db = sqlite3.connect(":memory:")
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(schema)
            db.executemany("INSERT INTO tile VALUES(?,?,?,?,?,?)", base_rows)
            if flavor == "left":
                db.execute("UPDATE tile SET title='左侧标题', ratio=9 WHERE slot=1")
                db.execute("UPDATE tile SET note='左支备注' WHERE slot=2")
            elif flavor == "right":
                db.execute("UPDATE tile SET title='右侧标题', payload=x'ff00' WHERE slot=1")
                db.execute("UPDATE tile SET note=NULL WHERE slot=2")
            db.commit()
            return db.serialize()
        finally:
            db.close()

    return {role: build(role) for role in ("base", "left", "right")}


def manual_run(client):
    """同一人工选择贯通 JSON、网页、失败请求与凭证。"""
    blobs = manual_images()
    ids = {}
    for role, data in blobs.items():
        ids[role] = client.call("/api/images", data, 201,
                                {"Content-Type": "application/octet-stream",
                                 "X-Image-Name": f"manual-{role}"})["id"]
    sources = {"base": ids["base"], "left": ids["left"], "right": ids["right"]}
    plan = client.call("/api/plans", sources, 201)
    route = f"/api/plans/{plan['id']}"
    expect(plan["unresolved"] == 2 and len(plan["items"]) == 2, "人工行示例应有两项冲突")
    first = next(i for i in plan["items"] if i["key"] == [["i", "1"]])
    second = next(i for i in plan["items"] if i["key"] == [["i", "2"]])

    blob = base64.b64encode(bytes.fromhex("0014ff")).decode()
    # 组合祖本（slot/code）与新手工值（title/note/payload/ratio）的合成行。
    row1 = [["i", "1"], ["t", "alpha"], ["t", "人工合流标题"], ["t", ""],
            ["b", blob], ["r", (2.75).hex()]]

    page = client.call(f"/plans/{plan['id']}")
    expect(b'value="manual"' in page and b'name="mt:0"' in page and "人工合成一行".encode() in page,
           "网页未提供人工合成行编辑器")

    # —— 输入转换/对齐失败：裁决不被接受，版本不动 ——
    bad_missing = client.call(route + "/decisions",
                              {"revision": 0, "item": first["id"], "choice": "manual"}, 422)
    expect(bad_missing["code"] == "manual_row" and bad_missing["stage"] == "decision_input",
           "缺少人工行未在输入步骤拒绝")
    bad_int = client.call(route + "/decisions", {"revision": 0, "item": first["id"], "choice": "manual",
                                                 "row": [["i", "x"], ["t", "alpha"], ["n"], ["n"], ["n"], ["n"]]}, 422)
    expect(bad_int["code"] == "manual_value" and bad_int["column"] == "slot", "整数转换失败未定位列")
    bad_blob = client.call(route + "/decisions",
                           {"revision": 0, "item": first["id"], "choice": "manual",
                            "row": [["i", "1"], ["t", "alpha"], ["n"], ["n"], ["b", "@@"], ["n"]]}, 422)
    expect(bad_blob["code"] == "manual_value" and bad_blob["column"] == "payload", "BLOB 转换失败未定位列")
    bad_key = client.call(route + "/decisions",
                          {"revision": 0, "item": first["id"], "choice": "manual",
                           "row": [["i", "9"], ["t", "alpha"], ["n"], ["n"], ["n"], ["n"]]}, 422)
    expect(bad_key["code"] == "manual_key" and bad_key["submitted_key"] == [["i", "9"]],
           "改动主键的裁决未被拒绝")
    expect(client.call(route)["revision"] == 0 and client.call(route)["unresolved"] == 2,
           "被拒绝的输入不应推进版本或改变待决")

    # —— 另一窗口先提交：旧版本被拒，并能看到当前版本 ——
    expect(client.call(route + "/decisions",
                       {"revision": 0, "item": second["id"], "choice": "left"})["revision"] == 1,
           "另一窗口的裁决未生效")
    stale = client.call(route + "/decisions",
                        {"revision": 0, "item": first["id"], "choice": "manual", "row": row1}, 409)
    expect(stale["code"] == "stale_revision" and stale["current_revision"] == 1
           and stale["item"] == first["id"], "过期人工裁决缺少可追溯信息")

    # —— 刷新版本后同一人工选择被接受；同值重试幂等 ——
    accepted = client.call(route + "/decisions",
                           {"revision": 1, "item": first["id"], "choice": "manual", "row": row1})
    expect(accepted["revision"] == 2 and accepted["row"] == row1, "人工合成行未按类型化值落库")
    again = client.call(route + "/decisions",
                        {"revision": 2, "item": first["id"], "choice": "manual", "row": row1})
    expect(again["revision"] == 2, "同一人工行重试推进了版本")

    # —— JSON 面可追溯 ——
    detail = client.call(route)
    item1 = next(i for i in detail["items"] if i["id"] == first["id"])
    expect(detail["revision"] == 2 and item1["choice"] == "manual"
           and item1["selected_row"] == row1, "计划 JSON 未回显同一人工行")
    entry1 = next(c for c in detail["choices"] if c["item"] == first["id"])
    expect(entry1["choice"] == "manual" and entry1["row"] == row1
           and entry1["digest"] == fingerprint({"choice": "manual", "row": row1}),
           "计划 JSON 的选择摘要不正确")
    expect(detail["decision_digest"] == fingerprint(
        {first["id"]: {"choice": "manual", "row": row1}, second["id"]: "left"}),
        "decision_digest 未覆盖人工裁决")

    # —— 网页面可追溯同一选择 ——
    page = client.call(f"/plans/{plan['id']}")
    for needle in (b'class="manual-selection"', first["id"].encode(), entry1["digest"][:16].encode(),
                   "人工合流标题".encode(), blob.encode(), "空文本".encode(),
                   b'value="manual" selected'):
        expect(needle in page, f"计划网页缺少人工选择痕迹：{needle!r}")

    # —— 网页表单（非 JSON、不跟随跳转）与表单失败页 ——
    form_status, bad_form = client.post_form(f"/plans/{plan['id']}/decisions", {
        "revision": "2", "item": first["id"], "choice": "manual",
        "mt:0": "i", "mv:0": "8", "mt:1": "t", "mv:1": "alpha", "mt:2": "n", "mv:2": "",
        "mt:3": "n", "mv:3": "", "mt:4": "n", "mv:4": "", "mt:5": "n", "mv:5": ""})
    expect(form_status == 422 and b"manual_key" in bad_form and "主键".encode() in bad_form,
           "网页表单改键未得到可定位的失败页")
    form_plan = client.call("/api/plans", sources, 201)
    fs, _ = client.post_form(f"/plans/{form_plan['id']}/decisions", {
        "revision": "0", "item": first["id"], "choice": "manual",
        "mt:0": "i", "mv:0": "1", "mt:1": "t", "mv:1": "alpha",
        "mt:2": "t", "mv:2": "表单合成标题", "mt:3": "t", "mv:3": "",
        "mt:4": "b", "mv:4": blob, "mt:5": "r", "mv:5": "2.75"})
    expect(fs == 303, f"合法网页表单未 303：{fs}")
    form_detail = client.call(f"/api/plans/{form_plan['id']}")
    form_item = next(i for i in form_detail["items"] if i["id"] == first["id"])
    expect(form_item["choice"] == "manual" and form_item["selected_row"][2] == ["t", "表单合成标题"],
           "网页表单的人工行未被接受")
    form_page = client.call(f"/plans/{form_plan['id']}")
    expect("表单合成标题".encode() in form_page and b'value="manual" selected' in form_page,
           "保存后回到计划页未回显人工行（离开再回来丢失）")

    # —— 重演步骤：转换合法但违反唯一约束，须与输入失败区分 ——
    row2_bad = [["i", "2"], ["t", "alpha"], ["t", "撞唯一码"], ["t", "x"], ["n"], ["r", "1"]]
    saved2 = client.call(route + "/decisions",
                         {"revision": 2, "item": second["id"], "choice": "manual", "row": row2_bad})
    expect(saved2["revision"] == 3, "第二个人工行未保存")
    blocked = client.call(route + "/rehearse", {"revision": 3})
    expect(blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint"
           and blocked["problem"].get("stage") == "rehearse"
           and blocked["problem"].get("table") == "tile",
           "重演约束冲突未标记为重演步骤")
    expect("candidate_digest" not in blocked, "受阻重演不应产生候选")

    # —— 修正第二行：空文本与 NULL 分别通过重演的写后读比对 ——
    row2_empty = [["i", "2"], ["t", "beta"], ["t", "贝塔修订"], ["t", ""], ["b", ""], ["r", "3.5"]]
    rev4 = client.call(route + "/decisions",
                       {"revision": 3, "item": second["id"], "choice": "manual", "row": row2_empty})["revision"]
    expect(rev4 == 4 and client.call(route + "/rehearse", {"revision": 4})["status"] == "passed",
           "空文本/空 BLOB 人工行重演未通过")
    row2_null = [["i", "2"], ["t", "beta"], ["t", "贝塔修订"], ["n"], ["b", ""], ["r", "3.5"]]
    rev5 = client.call(route + "/decisions",
                       {"revision": 4, "item": second["id"], "choice": "manual", "row": row2_null})["revision"]
    passed = client.call(route + "/rehearse", {"revision": 5})
    expect(passed["status"] == "passed", "NULL 人工行重演未通过")

    # —— 凭证绑定同一选择 ——
    cert = client.call(route + "/seal", {"revision": 5})
    plan_now = client.call(route)
    expect(cert["revision"] == 5 and cert["decision_digest"] == plan_now["decision_digest"],
           "凭证的裁决摘要与计划不一致")
    cert_first = next(c for c in cert["choices"] if c["item"] == first["id"])
    expect(cert_first["choice"] == "manual" and cert_first["row"] == row1
           and cert_first["digest"] == entry1["digest"], "凭证未追溯到同一人工选择")
    expect(cert["manual_rows"][first["id"]] == row1
           and cert["decision_digest"] == fingerprint(cert["decisions"]),
           "凭证的 manual_rows 或 decisions 原像不能复核 decision_digest")
    expect(client.call(route + "/seal", {"revision": 99}, 409)["code"] == "stale_revision",
           "旧版本封存未拒绝")

    # —— 下载库实际存储类型复核 ——
    candidate = client.call(route + "/download")
    expect(hashlib.sha256(candidate).hexdigest() == cert["candidate_digest"], "候选与凭证摘要不符")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], "结果库存在外键问题")
        one = db.execute("SELECT code,title,note,typeof(note),payload,ratio FROM tile WHERE slot=1").fetchone()
        expect(one[0] == "alpha" and one[1] == "人工合流标题" and one[2] == "" and one[3] == "text"
               and one[4] == bytes.fromhex("0014ff") and abs(one[5] - 2.75) < 1e-9,
               f"祖本+手工合成行落库值或类型错误：{one!r}")
        two = db.execute("SELECT note, payload, typeof(payload) FROM tile WHERE slot=2").fetchone()
        expect(two[0] is None and two[1] == b"" and two[2] == "blob",
               f"NULL 与空 BLOB 未分别保真：{two!r}")
        expect(db.execute("SELECT count(*) FROM tile").fetchone()[0] == 3, "对齐主键导致行数异常")
    finally:
        db.close()
    print("manual ✓ 人工合成行贯通网页表单、JSON、转换/版本/约束失败与凭证追溯")


def main():
    parser = argparse.ArgumentParser(description="验证 Pagefold 生产工作流")
    parser.add_argument("workflow",
                        choices=("all", *WORKFLOWS, "manual-row", "r024"))
    with tempfile.TemporaryDirectory(prefix="pagefold-walkthrough-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                goal = parser.parse_args().workflow
                client = Client(server.server_port)
                if goal in ("manual", "manual-row", "r024"):
                    manual_run(client)
                else:
                    run(client, goal)
                    if goal == "all":
                        manual_run(client)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
