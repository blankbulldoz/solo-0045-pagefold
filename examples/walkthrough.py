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

from pagefold.demo import identity_base, image, type_shifted, vacuum_copy
from pagefold.server import make_server
from pagefold.store import Store

WORKFLOWS = ("import", "catalog", "rows", "plan", "review", "decide", "rehearse", "attempts", "seal",
             "certificate", "download", "archive", "identity")


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


def identity_checks(client):
    """两种身份：VACUUM 等价件、类型漂移件，以及旧镜像登记新件后的显示与凭证。"""
    raw_base = identity_base()
    raw_vacuum = vacuum_copy(raw_base)
    raw_shifted = type_shifted()
    expect(raw_base != raw_vacuum, "VACUUM 样本的文件字节没有变化")

    def upload(name, blob):
        return client.call("/api/images", blob, 201,
                           {"X-Image-Name": name, "Content-Type": "application/octet-stream"})

    old = upload("identity-old", raw_base)
    # 旧镜像先被计划使用并封存。
    first = client.call("/api/plans", {"base": old["id"], "left": old["id"], "right": old["id"]}, 201)
    expect(client.call(f"/api/plans/{first['id']}/rehearse", {"revision": 0})["status"] == "passed",
           "身份样本空计划重演失败")
    certificate_before = client.call(f"/api/plans/{first['id']}/seal", {"revision": 0})

    # 此后才登记 VACUUM 件和类型漂移件。
    vacuum = upload("identity-vacuum", raw_vacuum)
    shifted = upload("identity-type-shift", raw_shifted)
    expect([item["id"] for item in vacuum.get("equivalents", [])] == [old["id"]],
           "登记响应未即时归组碎片等价件")
    expect(old["id"] != vacuum["id"], "VACUUM 后文件摘要竟然相同")
    expect(old["content_digest"] == vacuum["content_digest"], "碎片整理等价件的内容摘要不同")
    expect(old["content_digest"] != shifted["content_digest"], "整数/文本、NULL/空 BLOB 漂移被误判等价")
    duplicate = upload("identity-old-duplicate", raw_base)
    expect(duplicate["id"] == old["id"] and duplicate.get("reused") is True, "字节重复上传未标记复用")

    route = f"/api/images/{old['id']}"
    dossier = client.call(route)
    expect([item["id"] for item in dossier["equivalents"]] == [vacuum["id"]], "等价来源未归组")
    expect(any(item["id"] == shifted["id"] for item in dossier["type_lookalikes"]),
           "行位置相同但类型不同的来源未列出")
    used_first = next((item for item in dossier["usage"] if item["id"] == first["id"]), None)
    expect(used_first is not None and "base" in used_first["roles"]
           and used_first["certificate"] and used_first["certificate"]["candidate_digest"],
           "旧镜像的使用计划或历史凭证未显示")
    twin_dossier = client.call(f"/api/images/{vacuum['id']}")
    inherited = next((item for item in twin_dossier.get("equivalent_usage", [])
                      if item["id"] == first["id"]), None)
    expect(inherited and inherited["certificate"] and inherited["image_id"] == old["id"],
           "新登记的等价件未能顺内容身份回查旧镜像凭证")
    twin_page = client.call(f"/images/{vacuum['id']}")
    expect("内容等价的旧登记件历史来源".encode() in twin_page, "等价件页面未展示旧登记件历史")

    same = client.call(f"/api/images/compare?a={old['id']}&b={vacuum['id']}")
    expect(same["verdict"] == "defragmented_equivalent" and same["content_equivalent"] is True
           and same["byte_identical"] is False and same["positions_match"] is True,
           "碎片整理等价对照判定错误")
    drift = client.call(f"/api/images/compare?a={old['id']}&b={shifted['id']}")
    path_drift = client.call(f"/api/images/{old['id']}/compare/{shifted['id']}")
    expect(path_drift["verdict"] == drift["verdict"], "路径式身份对照入口不一致")
    kinds = {(change["kind_a"], change["kind_b"]) for change in drift["type_changes"]}
    expect(drift["verdict"] == "type_shifted_lookalike" and drift["positions_match"] is True
           and {("整数", "文本"), ("NULL", "空 BLOB")} <= kinds, "类型漂移对照未保真展示")

    # 旧镜像的显示与凭证在新件登记后保持不变。
    expect(client.call(f"/api/images/{old['id']}")["id"] == old["id"], "旧镜像无法按原摘要回查")
    certificate_after = client.call(f"/api/plans/{first['id']}/certificate")
    expect(certificate_after == certificate_before, "新镜像登记改变了历史凭证")
    expect(set(certificate_after["sources"].values()) == {old["id"]}, "历史凭证的精确文件来源被改写")

    page = client.call(f"/images/{old['id']}")
    for token in ("两种身份".encode(), "内容身份".encode(), "碎片整理后等价的来源".encode(),
                  vacuum["id"][:16].encode(), "行位置相同但类型不同的来源".encode(),
                  "空 BLOB".encode(), "使用该镜像的计划与历史凭证".encode(), first["id"][:12].encode()):
        expect(token in page, f"镜像页缺少身份展示：{token!r}")
    expect("整数".encode() in page and "文本".encode() in page, "镜像页缺少整数/文本类型对照")
    compare_page = client.call(f"/images/compare?a={old['id']}&b={shifted['id']}")
    expect("类型变更".encode() in compare_page and "空 BLOB".encode() in compare_page,
           "对照页未展示类型差异")

    # 计划选源：三个角色携带两种身份；等价件换角色不改变三方计划内容。
    sources = {"base": old["id"], "left": vacuum["id"], "right": shifted["id"]}
    plan = client.call("/api/plans", sources, 201)
    identities = plan["source_identities"]
    expect(identities["base"]["content_digest"] == identities["left"]["content_digest"]
           and identities["right"]["content_digest"] != identities["base"]["content_digest"],
           "计划来源身份标注错误")
    swapped = client.call("/api/plans", {"base": vacuum["id"], "left": old["id"],
                                         "right": shifted["id"]}, 201)
    expect(swapped["plan_digest"] == plan["plan_digest"], "内容等价来源改变了计划摘要")
    plan_page = client.call(f"/plans/{plan['id']}")
    expect("来源身份对照".encode() in plan_page and "碎片整理后等价".encode() in plan_page
           and "行位置相同但类型不同".encode() in plan_page, "计划页未展示来源身份关系")
    passed = client.call(f"/api/plans/{plan['id']}/rehearse", {"revision": 0})
    expect(passed["status"] == "passed", "VACUUM 等价件作为来源重演失败")
    sealed = client.call(f"/api/plans/{plan['id']}/seal", {"revision": 0})
    expect(sealed["sources"] == sources, "新凭证未精确绑定三个文件摘要")
    expect(sealed["source_content"]["base"] == sealed["source_content"]["left"]
           and sealed["source_content"]["right"] != sealed["source_content"]["base"],
           "凭证的内容身份绑定错误")
    expect(hashlib.sha256(client.call(f"/api/plans/{plan['id']}/download")).hexdigest()
           == sealed["candidate_digest"], "身份样本候选下载摘要不一致")


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
    if goal in ("identity", "all"):
        identity_checks(client)
    if done("identity", "文件/内容两种身份可查，碎片等价与类型漂移清楚区分，旧镜像凭证不变"):
        return


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
