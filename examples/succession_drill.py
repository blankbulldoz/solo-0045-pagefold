"""承接归档演练：通过真实 HTTP 入口验证可选承接流程。

覆盖：
1. 可承接计划：旧手工选择在更正祖本后逐条核对，retained 选择自动写入承接计划。
2. 需要重审的计划：来源行变化、差异自动化、条目消失分别落入 needs_review/automatic/dropped。
3. 归档并建立承接关系中途失败时事务整体回滚，不留下半成品；既有封存成果仍可提取。
"""
import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pagefold.demo import succession_image
from pagefold.server import make_server
from pagefold.store import Store

OLD_ROLES = ("bad", "left", "right")
NEW_ROLES = ("fixed", "left_fixed", "right_fixed")
# 旧计划四个冲突键的手工选择，覆盖四种承接去向。
OLD_CHOICES = {"north": "right", "south": "base", "east": "right", "west": "base"}


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200):
        headers = {}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=data, headers=headers)
        try:
            with urlopen(request, timeout=10) as result:
                body, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                body, actual, kind = error.read(), error.code, error.headers.get_content_type()
        expect(actual == status, f"{route}: {actual} != {status}: {body[:300]}")
        return json.loads(body) if kind == "application/json" else body

    def upload(self, name):
        request = Request(self.origin + "/api/images", succession_image(name),
                          headers={"X-Image-Name": name, "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            expect(result.status == 201, f"上传 {name} 失败")
            return json.loads(result.read())

    def upload_bytes(self, name, data):
        request = Request(self.origin + "/api/images", data,
                          headers={"X-Image-Name": name, "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            expect(result.status == 201, f"上传 {name} 失败")
            return json.loads(result.read())


def mismatched_image():
    """在演练镜像上追加一个索引，使模式签名与原计划不一致。"""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(succession_image("fixed"))
        db.execute("CREATE INDEX bench_label_extra ON bench(label)")
        return db.serialize()
    finally:
        db.close()


def build_old_plan(client, label):
    images = {role: client.upload(kind)["id"]
              for role, kind in zip(("base", "left", "right"), OLD_ROLES)}
    plan = client.call("/api/plans", images, 201)
    expect(len(plan["items"]) == 4 and plan["unresolved"] == 4, f"{label} 应产生四个冲突条目")
    revision = plan["revision"]
    for item in plan["items"]:
        name = item["key"][0][1]
        answer = client.call(f"/api/plans/{plan['id']}/decisions",
                             {"revision": revision, "item": item["id"], "choice": OLD_CHOICES[name]})
        revision = answer["revision"]
    plan = client.call(f"/api/plans/{plan['id']}")
    expect(plan["unresolved"] == 0 and plan["revision"] == 4, f"{label} 手工裁决未全部保存")
    return images, plan


def corrected_sources(client, old_images):
    return {role: client.upload(kind)["id"]
            for role, kind in zip(("base", "left", "right"), NEW_ROLES)}


def run(client):
    print("== 1. 可承接计划：手工选择逐条保留 ==")
    old_images, old_plan = build_old_plan(client, "可承接计划")
    # 先重演并封存，验证承接归档不销毁已封存成果。
    passed = client.call(f"/api/plans/{old_plan['id']}/rehearse", {"revision": old_plan["revision"]})
    expect(passed["status"] == "passed", "可承接计划重演应通过")
    certificate = client.call(f"/api/plans/{old_plan['id']}/seal", {"revision": old_plan["revision"]})
    candidate = client.call(f"/api/plans/{old_plan['id']}/download")
    expect(hashlib.sha256(candidate).hexdigest() == certificate["candidate_digest"], "封存候选摘要不匹配")

    sources = corrected_sources(client, old_images)
    archived = client.call(f"/api/plans/{old_plan['id']}/archive",
                           {"revision": old_plan["revision"], **sources})
    expect(archived["state"] == "archived" and archived["successor"], "归档应返回承接计划标识")
    report = archived["report"]
    by_key = {(entry["table"], entry["key"][0][1]): entry for entry in report["entries"]}
    expect(by_key[("node", "north")]["status"] == "retained", "north 应保留旧选择")
    expect(by_key[("node", "south")]["status"] == "needs_review", "south 应要求重审")
    expect(by_key[("node", "east")]["status"] == "automatic", "east 应已自动化")
    expect(by_key[("node", "west")]["status"] == "dropped", "west 条目应消失")
    print("  承接报告：", json.dumps(report["summary"], ensure_ascii=False))

    successor = client.call(f"/api/plans/{archived['successor']}")
    expect(successor["state"] == "open", "承接计划应处于可裁决状态")
    expect(successor["succession"]["relation"] == "successor", "承接关系方向不正确")
    north = next(i for i in successor["items"] if i["key"][0][1] == "north")
    expect(north["choice"] == "right", "retained 选择没有写入承接计划")
    expect(successor["revision"] == 1, "承接写入应把承接计划推进到第 1 版")
    review = [i for i in successor["items"] if i["automatic"] is None and i["choice"] is None]
    expect(len(review) == 1 and review[0]["key"][0][1] == "south", "仅 south 应留待重审")
    # 承接计划可以直接继续工作流：补裁后重演通过。
    answer = client.call(f"/api/plans/{successor['id']}/decisions",
                         {"revision": successor["revision"], "item": review[0]["id"], "choice": "left"})
    fresh = client.call(f"/api/plans/{successor['id']}/rehearse", {"revision": answer["revision"]})
    expect(fresh["status"] == "passed", "承接计划补裁后重演应通过")
    print(f"  承接计划 {successor['id'][:12]} 补裁后重演通过，候选 {fresh['candidate_digest'][:16]}…")

    # 既有封存成果在归档后仍可提取，且字节不变。
    again_candidate = client.call(f"/api/plans/{old_plan['id']}/download")
    again_cert = client.call(f"/api/plans/{old_plan['id']}/certificate")
    expect(again_candidate == candidate and again_cert == certificate, "归档后封存成果发生变化")
    print("  归档后原计划封存的 SQLite 与凭证仍可原样提取。")

    print("== 2. 需要重审的计划：四种去向逐条核对 ==")
    _, review_plan = build_old_plan(client, "重审计划")
    sources_two = corrected_sources(client, old_images)
    archived_two = client.call(f"/api/plans/{review_plan['id']}/archive",
                               {"revision": review_plan["revision"], **sources_two})
    report_two = archived_two["report"]
    statuses = sorted(entry["status"] for entry in report_two["entries"])
    expect(statuses == ["automatic", "dropped", "needs_review", "retained"], f"承接去向不全：{statuses}")
    successor_two = client.call(f"/api/plans/{archived_two['successor']}")
    expect(successor_two["unresolved"] == 1, "重审计划应恰好留下一个未决冲突")
    carry_record = client.call(f"/api/plans/{review_plan['id']}/carryover")
    expect(carry_record["successor"] == archived_two["successor"], "承接报告查询入口不一致")
    print("  逐条核对：")
    for entry in report_two["entries"]:
        print(f"    node {entry['key'][0][1]:<5} 旧选 {entry['previous_choice']:<5} → {entry['status']:<12} {entry['reason']}")

    print("== 3. 中途失败不做一半，封存结果仍可提取 ==")
    _, fail_plan = build_old_plan(client, "失败演练计划")
    fail_cert = client.call(f"/api/plans/{fail_plan['id']}/seal",
                            {"revision": fail_plan["revision"]}, status=409)
    expect(fail_cert["code"] == "rehearsal_required", "未重演计划不能封存")
    fail_passed = client.call(f"/api/plans/{fail_plan['id']}/rehearse", {"revision": fail_plan["revision"]})
    expect(fail_passed["status"] == "passed", "失败演练计划重演应通过")
    fail_certificate = client.call(f"/api/plans/{fail_plan['id']}/seal",
                                   {"revision": fail_plan["revision"]})
    fail_candidate = client.call(f"/api/plans/{fail_plan['id']}/download")
    before = client.call(f"/api/plans/{fail_plan['id']}")
    plan_count_before = len(client.call("/api/plans"))

    blown = client.call(f"/api/plans/{fail_plan['id']}/archive",
                        {"revision": fail_plan["revision"], "drill_fail": "1", **sources_two}, status=500)
    expect(blown["code"] == "drill_failure", f"演练注入失败未按预期返回：{blown}")
    after = client.call(f"/api/plans/{fail_plan['id']}")
    expect(after["state"] == before["state"] and after["revision"] == before["revision"],
           "失败后旧计划被改动")
    expect(after["state"] == "sealed", "失败演练前已封存的状态应保留")
    expect(after["succession"] is None, "失败后仍写入了承接关系")
    plan_count_after = len(client.call("/api/plans"))
    expect(plan_count_after == plan_count_before, "失败后留下了半成品承接计划")
    print("  注入失败后：旧计划保持已封存状态，承接计划与承接关系均未写入。")

    # 自然失败：过期版本与模式不一致同样不得留下半成品（此时计划仍为已封存）。
    stale = client.call(f"/api/plans/{fail_plan['id']}/archive",
                        {"revision": fail_plan["revision"] + 1, **sources_two}, status=409)
    expect(stale["code"] == "stale_revision", "过期版本未拒绝")
    expect(len(client.call("/api/plans")) == plan_count_before, "过期请求产生了计划")
    extra = client.upload_bytes("fixed-extra-index", mismatched_image())
    bad_source = dict(sources_two, base=extra["id"])
    mismatch = client.call(f"/api/plans/{fail_plan['id']}/archive",
                           {"revision": fail_plan["revision"], **bad_source}, status=409)
    expect(mismatch["code"] == "schema_mismatch", "模式不一致未拒绝")
    expect(len(client.call("/api/plans")) == plan_count_before, "模式不一致请求产生了计划")
    still_sealed = client.call(f"/api/plans/{fail_plan['id']}")
    expect(still_sealed["state"] == "sealed" and still_sealed["succession"] is None,
           "自然失败改动了原计划")

    # 失败后原计划仍可正常归档（这次不带承接），封存成果继续可提取。
    plain = client.call(f"/api/plans/{fail_plan['id']}/archive", {"revision": fail_plan["revision"]})
    expect(plain["state"] == "archived", "失败演练后无法正常归档")
    expect(client.call(f"/api/plans/{fail_plan['id']}/download") == fail_candidate, "失败演练封存候选丢失")
    expect(client.call(f"/api/plans/{fail_plan['id']}/certificate") == fail_certificate, "失败演练凭证丢失")
    print("  恢复后正常归档；既有 SQLite 成果与凭证字节一致，仍可下载。")

    print("== 4. 归档幂等与边界 ==")
    again = client.call(f"/api/plans/{old_plan['id']}/archive", {"revision": old_plan["revision"]})
    expect(again["successor"] == archived["successor"], "重复归档未稳定返回同一承接关系")
    closed = client.call(f"/api/plans/{old_plan['id']}/archive",
                         {"revision": old_plan["revision"], **sources}, status=409)
    expect(closed["code"] == "closed_plan", "已归档计划不应再建立承接")

    # 无承接关系的查询返回 404。
    _, bare_plan = build_old_plan(client, "边界计划")
    missing = client.call(f"/api/plans/{bare_plan['id']}/carryover", status=404)
    expect(missing["code"] == "no_succession", "无承接关系应返回 404")

    # 仅给出更正祖本，分支留空时沿用当前镜像。
    partial = client.call(f"/api/plans/{bare_plan['id']}/archive",
                          {"revision": bare_plan["revision"], "base": sources["base"]})
    partial_successor = client.call(f"/api/plans/{partial['successor']}")
    expect(partial_successor["sources"]["left"] == old_images["left"]
           and partial_successor["sources"]["right"] == old_images["right"]
           and partial_successor["sources"]["base"] == sources["base"], "留空角色未沿用当前镜像")

    # 非法镜像摘要在写入前被拒绝，不产生新计划。
    _, guarded_plan = build_old_plan(client, "校验计划")
    plan_count = len(client.call("/api/plans"))
    bad_token = client.call(f"/api/plans/{guarded_plan['id']}/archive",
                            {"revision": guarded_plan["revision"], "base": "z" * 64,
                             "left": sources["left"], "right": sources["right"]}, status=400)
    expect(bad_token["code"] == "token", "非法镜像摘要未拒绝")
    expect(len(client.call("/api/plans")) == plan_count, "非法请求产生了计划")

    # 无手工裁决的计划也可承接：报告为空，承接计划从第 0 版开始。
    empty_images = {role: client.upload(kind)["id"]
                    for role, kind in zip(("base", "left", "right"), OLD_ROLES)}
    empty_plan = client.call("/api/plans", empty_images, 201)
    empty_archived = client.call(f"/api/plans/{empty_plan['id']}/archive",
                                 {"revision": 0, **sources})
    expect(empty_archived["report"]["entries"] == []
           and empty_archived["report"]["summary"] == {
               "retained": 0, "needs_review": 0, "automatic": 0, "dropped": 0}, "空裁决承接报告不正确")
    expect(client.call(f"/api/plans/{empty_archived['successor']}")["revision"] == 0, "空承接计划版本不为 0")

    # 仅归档（不承接）可重复调用且不附带承接标识；归档后再补承接被拒绝。
    plain_images = {role: client.upload(kind)["id"]
                    for role, kind in zip(("base", "left", "right"), OLD_ROLES)}
    plain_plan = client.call("/api/plans", plain_images, 201)
    once = client.call(f"/api/plans/{plain_plan['id']}/archive", {"revision": 0})
    expect("successor" not in once and once["state"] == "archived", "仅归档不应建立承接")
    twice = client.call(f"/api/plans/{plain_plan['id']}/archive", {"revision": 0})
    expect("successor" not in twice and twice["state"] == "archived", "重复仅归档不稳定")
    late = client.call(f"/api/plans/{plain_plan['id']}/archive",
                       {"revision": 0, **sources}, status=409)
    expect(late["code"] == "closed_plan", "归档后补建承接未被拒绝")
    print("全部承接演练通过。")


def main():
    argparse.ArgumentParser(description="验证 Pagefold 承接归档流程").parse_args()
    with tempfile.TemporaryDirectory(prefix="pagefold-succession-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(Client(server.server_port))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
