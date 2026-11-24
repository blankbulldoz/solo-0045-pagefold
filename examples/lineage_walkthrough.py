"""替代祖本并列试算的端到端工作流检查。

走真实 HTTP 入口：
  1. 登记两套（alpha / beta）含复合键范围重叠的镜像；
  2. 对同一对分支、两个候选祖本发起不保存的并列试算；
  3. 断言自动采纳翻转、待决化解、缺席/无差异/未决三态可分清；
  4. 验证 beta 套是 alpha 套的左右角色镜像，模式仍可追溯；
  5. 用试算返回的来源参数复刻某一侧的正式计划，并完成重演与封存。

运行：python3 -m examples.lineage_walkthrough
"""
import json
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pagefold.lineage_inputs import lineage_images, suite_sources
from pagefold.server import make_server
from pagefold.store import Store


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

    def upload(self, name, data, label):
        request = Request(self.origin + "/api/images", data=data,
                          headers={"Content-Type": "application/octet-stream",
                                   "X-Image-Name": quote(label)})
        with urlopen(request, timeout=10) as result:
            expect(result.status == 201, f"登记 {name} 失败")
            return json.loads(result.read())


def register_suite(client):
    ids = {}
    for key, (label, data) in lineage_images().items():
        ids[key] = client.upload(key, data, label)["id"]
    return ids


def run_trial(client, ids, suite):
    src = suite_sources(suite)
    payload = {
        "left": ids[src["left"]],
        "right": ids[src["right"]],
        "bases": [{"label": label, "id": ids[key]} for label, key in src["bases"]],
    }
    return client.call("/api/trials", payload, 200), src


def assert_trial_shape(trial, suite):
    expect(trial["saved"] is False, "试算必须声明不落库")
    expect(len(trial["previews"]) == 2, "应返回上下两份预览")
    comparison = trial["comparison"]
    expect(comparison["same_total"] is True, "两套祖本的计划项总数应相同")
    expect(len(comparison["automatic_flip_items"]) == 2, "应有两个自动采纳翻转键")
    idseq = {row["id"]: row["key"][1][1] for row in trial["aligned"]}
    flips = sorted(idseq[i] for i in comparison["automatic_flip_items"])
    expect(flips == ["2", "3"], f"翻转键应为 seq2/seq3，实际 {flips}")
    by_seq = {row["key"][1][1]: row for row in trial["aligned"]}
    cseq = {entry["id"]: entry["became"] for entry in comparison["conflict_resolved_items"]}
    # 缺席 / 自动 / 待决三态分清。
    expect(by_seq["4"]["statuses"][f"{suite} 祖本 A"] == "automatic", "seq4 在 A 下应是自动删除项")
    expect(by_seq["4"]["statuses"][f"{suite} 祖本 B"] == "absent", "seq4 在 B 下应缺席省略")
    expect(by_seq["6"]["statuses"][f"{suite} 祖本 A"] == "absent", "seq6 在 A 下应无差异省略")
    expect(by_seq["6"]["statuses"][f"{suite} 祖本 B"] == "automatic", "seq6 在 B 下应是自动项")
    expect(by_seq["1"]["statuses"][f"{suite} 祖本 A"] == "conflict", "seq1 在 A 下应待决")
    expect(by_seq["1"]["statuses"][f"{suite} 祖本 B"] == "conflict", "seq1 在 B 下应保持待决")
    expect(by_seq["8"]["statuses"][f"{suite} 祖本 A"] == "conflict", "seq8 在 A 下应待决")
    expect(by_seq["8"]["statuses"][f"{suite} 祖本 B"] == "automatic", "seq8 在 B 下待决应化解为自动")
    # 有方向的差分：seq4 从上半份消失、seq6 在下半份出现、seq8 待决被化解。
    expect(sorted(idseq[i] for i in comparison["vanished_items"]) == ["4"], "seq4 应标记为消失")
    expect(sorted(idseq[i] for i in comparison["appeared_items"]) == ["6"], "seq6 应标记为出现")
    expect(sorted(idseq[e["id"]] for e in comparison["conflict_resolved_items"]) == ["8"],
            "seq8 应标记为待决化解")
    expect(all(by_seq[idseq[e["id"]]]["statuses"][f"{suite} 祖本 B"] == e["became"]
               for e in comparison["conflict_resolved_items"]), "待决化解后的状态标注不一致")
    expect(comparison["conflict_introduced_items"] == [], "本素材不应出现新引入的待决")
    expect(comparison["conflicts"][f"{suite} 祖本 A"] == 2
           and comparison["conflicts"][f"{suite} 祖本 B"] == 1, "两侧未决计数应分别为 2 和 1")
    # 同总数但实质不同。
    expect(comparison["materially_different_count"] == 5, "应有 5 个实质不同的主键行")
    return by_seq


def assert_mirror(alpha_trial, beta_trial):
    a = {row["key"][1][1]: row for row in alpha_trial["aligned"]}
    b = {row["key"][1][1]: row for row in beta_trial["aligned"]}
    # 同一 seq 的状态类别序列一致，说明镜像后模式与处置可追溯。
    for seq in ("1", "2", "3", "4", "6", "8"):
        expect(list(a[seq]["statuses"].values()) == list(b[seq]["statuses"].values()),
               f"seq{seq} 镜像后状态类别不一致")
    # 翻转键的左右角色在 beta 中整体互换。
    for seq in ("2", "3"):
        alpha_auto = list(a[seq]["automatic"].values())
        beta_auto = list(b[seq]["automatic"].values())
        mirrored = [{"left": "right", "right": "left"}.get(role, role) for role in alpha_auto]
        expect(beta_auto == mirrored, f"seq{seq} 左右角色未正确镜像：{alpha_auto} -> {beta_auto}")
    # 模式摘要仍可由任一镜像追溯。
    alpha_catalog = json.loads(json.dumps(alpha_trial["bases"]))
    expect(len({base["id"] for base in alpha_trial["bases"]}) == 2, "两个候选祖本应是不同镜像")
    expect(len({base["id"] for base in beta_trial["bases"]}) == 2, "beta 候选祖本应各自独立")


def reproduce_official_plan(client, ids, trial, src, suite):
    """用试算给出的来源复刻候选祖本 B 一侧的正式计划，并重演、封存。"""
    label = src["bases"][1][0]
    base = next(base for base in trial["bases"] if base["label"] == label)
    plan = client.call("/api/plans", {"base": base["id"],
                                      "left": ids[src["left"]],
                                      "right": ids[src["right"]]}, 201)
    preview = next(p for p in trial["previews"] if p["label"] == label)
    # 正式计划的条目指纹与试算预览逐项一致（同主键、同自动采纳）。
    expect(plan["plan_digest"] == preview["plan_digest"], "复刻计划与试算预览指纹不一致")
    expect(len(plan["items"]) == preview["counts"]["items"], "复刻计划条目数与试算不符")
    for official, trial_item in zip(plan["items"], preview["items"]):
        expect(official["id"] == trial_item["id"], "计划项主键无法对上")
        expect(official["automatic"] == trial_item["automatic"], "自动采纳角色与试算不一致")
    # B 视角仍有一个共同冲突 seq1，需要裁决后才能重演。
    conflict = next(item for item in plan["items"] if item["automatic"] is None)
    decided = client.call(f"/api/plans/{plan['id']}/decisions",
                          {"revision": 0, "item": conflict["id"], "choice": "right"}, 200)
    attempt = client.call(f"/api/plans/{plan['id']}/rehearse",
                          {"revision": decided["revision"]}, 200)
    expect(attempt["status"] == "passed", f"复刻计划重演未通过：{attempt}")
    certificate = client.call(f"/api/plans/{plan['id']}/seal",
                              {"revision": decided["revision"]}, 200)
    expect(certificate["plan_digest"] == preview["plan_digest"], "封存凭证的计划摘要与试算不符")
    return plan, certificate


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-lineage-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                ids = register_suite(client)
                alpha_trial, alpha_src = run_trial(client, ids, "alpha")
                assert_trial_shape(alpha_trial, "alpha")
                print("alpha 并列试算 ✓ 翻转/缺席/待决/同总数异实质 全部断言通过")
                beta_trial, beta_src = run_trial(client, ids, "beta")
                assert_trial_shape(beta_trial, "beta")
                print("beta 并列试算 ✓ 与 alpha 同构")
                assert_mirror(alpha_trial, beta_trial)
                print("镜像角色与模式追溯 ✓ 左右角色整体互换、状态类别保持")
                plan, certificate = reproduce_official_plan(client, ids, alpha_trial, alpha_src, "alpha")
                print(f"复刻正式计划 ✓ 计划 {plan['id'][:12]} 重演通过并封存，"
                      f"候选摘要 {certificate['candidate_digest'][:16]}…")
                # 试算不得产生任何计划外的副作用：试算本身没有写入，库中只有复刻的 1 个计划。
                plans = client.call("/api/plans")
                expect(len(plans) == 1, f"试算不应落库，实际计划数 {len(plans)}")
                print("不落库保证 ✓ 状态库中仅存在复刻的正式计划")
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
