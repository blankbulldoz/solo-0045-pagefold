"""
两代合流成果的分支谱系与共同输入发现示例。

本脚本通过真实 HTTP 入口构造两代封存成果，集中展示四件容易混淆的事：

1. 共同输入：启动二代计划前，先预检三个候选镜像共享哪些上游来源。
2. 含糊或缺失的来源边：只有文件名沿用旧称（字节已改写）时只产生弱证据；
   操作者明确表示来源不明时登记缺失边。
3. 选定后正式计划的角色：计划一经创建，三个来源各自获得可核验的强角色边，
   同一文件可在不同计划里复用为不同角色。
4. 字节身份与来源声明的证据强度：重新导入与一代封存候选逐字节相同的文件时，
   自动得到强证据；操作者口头声明仅为弱证据；多条同等可信的强推导路线一并可查。

运行：python3 -m examples.lineage_demo [step]
step 取值：generation-one / reimport / reuse / preflight / generation-two / summary
"""
import argparse
import json
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pagefold.demo import lineage_image
from pagefold.server import make_server
from pagefold.store import Store

STEPS = (
    "generation-one",   # 第一代：三镜像、计划、角色边、裁决、重演、封存
    "reimport",         # 重新导入：字节身份强证据 vs 文件名弱证据 vs 缺失边
    "reuse",            # 同一文件在不同计划角色复用
    "preflight",        # 二代启动前的共同输入发现
    "generation-two",   # 第二代正式计划：角色边与多条同等可信路线
    "summary",          # 汇总谱系证据
)


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

    def upload_bytes(self, name, data, status=201, headers=None):
        merged = {"X-Image-Name": quote(name), "Content-Type": "application/octet-stream"}
        for key, value in (headers or {}).items():
            merged[key] = quote(value, safe=",:") if key.startswith("X-Lineage-") else value
        return self.call("/api/images", data, status, merged)

    def upload(self, name, flavor, status=201, headers=None):
        return self.upload_bytes(name, lineage_image(flavor), status, headers)

    def plan(self, sources, status=201, extra=None):
        return self.call("/api/plans", dict(sources, **(extra or {})), status)

    def lineage(self, image_id):
        return self.call(f"/api/images/{image_id}/lineage")

    def routes(self, image_id):
        return self.call(f"/api/images/{image_id}/lineage/routes")


def settle_conflict(client, plan, choice):
    route = f"/api/plans/{plan['id']}"
    conflict = next(item for item in plan["items"] if not item["automatic"])
    revision = client.call(route + "/decisions",
                           {"item": conflict["id"], "choice": choice,
                            "revision": plan["revision"]})["revision"]
    return client.call(route + "/rehearse", {"revision": revision}), revision


def run(client, stop_after):
    def done(step, result):
        print(f"{step} ✓ {result}")
        return stop_after == step

    # ── 第一代：登记三个同模式镜像并创建正式计划 ──────────────────────────────
    base = client.upload("一代祖本", "gen1-base")
    left = client.upload("一代左支", "gen1-left")
    right = client.upload("一代右支", "gen1-right")
    sources_one = {"base": base["id"], "left": left["id"], "right": right["id"]}
    plan_one = client.plan(sources_one)
    expect(plan_one["state"] == "open" and plan_one["unresolved"] >= 1, "一代计划应含待裁决冲突")
    # 计划角色边在创建时即写入，三个来源都可回查到自己的强角色边。
    for role, image_id in sources_one.items():
        edges = client.lineage(image_id)["edges"]
        role_edges = [e for e in edges if e["basis"] == "plan_role" and e["role"] == role]
        expect(role_edges and role_edges[0]["confidence"] == "strong",
               f"正式计划的{role}角色未登记为强证据")

    # 裁决、重演并封存第一代成果 C1。
    passed, revision = settle_conflict(client, plan_one, "right")
    expect(passed["status"] == "passed", "一代候选重演未通过")
    certificate = client.call(f"/api/plans/{plan_one['id']}/seal", {"revision": revision})
    c1_digest = certificate["candidate_digest"]
    expect(certificate["sources"] == sources_one, "封存凭证未精确绑定三个来源角色")
    if done("generation-one", "一代三镜像登记，正式计划写入三条强角色边并封存 C1"):
        return

    # ── 重新导入：三种证据强度并列 ────────────────────────────────────────────
    # (a) 字节身份：导入与封存候选 C1 逐字节相同的文件，自动识别为强证据。
    c1_bytes = client.call(f"/api/plans/{plan_one['id']}/download")
    client.upload_bytes("重新导入的封存成果", c1_bytes)
    sealed_lineage = client.lineage(c1_digest)
    sealed_edges = [e for e in sealed_lineage["edges"] if e["basis"] == "sealed_output"]
    expect(sealed_edges and all(e["confidence"] == "strong" for e in sealed_edges),
           "字节相同的重新导入未获得封存输出强证据")

    # (b) 文件名弱证据：标签沿用一代右支，字节却是另一套改写，摘要不同。
    decoy = client.upload("一代右支", "renamed")
    expect(decoy["id"] != right["id"], "改名镜像的字节摘要不应与原右支相同")
    decoy_lineage = client.lineage(decoy["id"])
    expect(not any(e["basis"] == "sealed_output" for e in decoy_lineage["edges"]),
           "字节不同的改名镜像不得取得封存强证据")
    filename_edges = [e for e in decoy_lineage["edges"] if e["basis"] == "filename"]
    expect(filename_edges and filename_edges[0]["confidence"] == "weak"
           and filename_edges[0]["source_image_id"] == right["id"],
           "同名改写镜像应登记指向原右支的文件名弱边，而非冒充字节身份")

    # (c) 缺失边：操作者明确声明来源不明（使用字节独立的 mystery 镜像）。
    mystery = client.upload("来源不明的封存件", "mystery",
                            headers={"X-Lineage-Missing": "交接清单未记录该文件出处"})
    mystery_lineage = client.lineage(mystery["id"])
    missing_edges = mystery_lineage["missing_evidence"]
    expect(missing_edges and missing_edges[0]["basis"] == "missing", "来源缺失边未登记")
    expect(mystery_lineage["strongest_confidence"] in (None, "weak", "none"),
           "来源不明镜像不应出现强证据")

    # (d) 声明弱证据：操作者口头声称两个上游，系统无法仅凭字节核验。
    declared = client.upload("带口头来源的分支", "gen2-left", headers={
        "X-Lineage-Claims": f"{base['id']},{c1_digest}"})
    declared_lineage = client.lineage(declared["id"])
    claim_edges = [e for e in declared_lineage["edges"] if e["basis"] == "declared"]
    expect(len(claim_edges) == 2 and all(e["confidence"] == "weak" for e in claim_edges),
           "操作者声明应登记为两条弱证据边")
    if done("reimport", "字节身份强证据、文件名弱证据、来源缺失边与口头声明分级登记"):
        return

    # ── 同一文件跨计划角色复用：C1 在一代是封存输出，在二代将作为祖本输入 ──────
    gen2_left = client.upload("二代左支", "gen2-left", headers={
        "X-Lineage-Claims": c1_digest})
    gen2_right = client.upload("二代右支", "gen2-right", headers={
        "X-Lineage-Claims": c1_digest})
    expect(any(e["basis"] == "sealed_output" for e in client.lineage(c1_digest)["edges"]),
           "复用前 C1 应已有封存输出边")
    if done("reuse", "C1 以同一字节身份准备在二代计划中换角色复用"):
        return

    # ── 二代启动前的共同输入预检（先不建计划）────────────────────────────────
    gen2_sources = {"base": c1_digest, "left": gen2_left["id"], "right": gen2_right["id"]}
    preflight = client.plan(gen2_sources, extra={"preflight": 1})
    expect("common_inputs" in preflight and "ambiguous_roles" in preflight,
           "预检未返回共同输入与含糊角色")
    # 两条二代分支都口头声明源自 C1，故 C1 是被两个角色共同指向的上游。
    common = {item["anchor"]: item for item in preflight["common_inputs"]}
    expect(c1_digest in common, "预检未发现二代分支共享的一代上游 C1")
    c1_common = common[c1_digest]
    expect(c1_common["max_confidence"] == "weak", "口头声明构成的共同入边应判为弱证据")
    # 但 C1 这个锚点镜像本身可由字节核验出强身份，两层证据分别报告。
    expect(c1_common["anchor_self_confidence"] == "strong",
           "锚点 C1 自身的字节强身份未与弱入边区分开")
    # 含糊角色：来源不明的镜像作为候选时，预检必须标出缺失边。
    ambiguous_check = client.plan(
        {"base": c1_digest, "left": mystery["id"], "right": gen2_right["id"]},
        extra={"preflight": 1})
    expect(ambiguous_check["ambiguous_count"] >= 1, "含缺失边的候选未被标为含糊")
    if done("preflight", "启动二代前发现共同输入，区分弱入边与锚点字节强身份并标出含糊候选"):
        return

    # ── 第二代正式计划：选定后写入角色边，C1 同时拥有两条同等可信强路线 ────────
    plan_two = client.plan(gen2_sources)
    c1_routes = client.routes(c1_digest)
    strong_basis = {e["basis"] for e in c1_routes["routes"]}
    expect(c1_routes["plural_strong_routes"], "C1 应同时具有封存输出与二代祖本两条强路线")
    expect({"sealed_output", "plan_role"} <= strong_basis,
           "多条同等可信的推导路线未一并可查")
    # 同一文件的角色复用：C1 在二代计划里是祖本角色。
    role_plans = {e["role"] for e in client.lineage(c1_digest)["edges"]
                  if e["basis"] == "plan_role"}
    expect("base" in role_plans, "C1 在二代计划的祖本角色未登记")
    if done("generation-two", "二代正式计划登记角色边，同一文件跨计划复用且强路线并列"):
        return

    # ── 汇总：证据强度分层，文件名不能冒充祖本 ───────────────────────────────
    summary = {"c1": client.lineage(c1_digest), "decoy": decoy_lineage,
               "mystery": mystery_lineage, "declared": declared_lineage}
    expect(summary["c1"]["strongest_confidence"] == "strong", "C1 最强证据应为强")
    expect(summary["decoy"]["strongest_confidence"] != "strong", "改名陷阱不得冒充强来源")
    expect(summary["mystery"]["has_missing_edge"], "来源缺失边应保留可查")
    expect([e for e in summary["declared"]["weak_evidence"] if e["basis"] == "declared"],
           "口头声明的弱证据应可与强证据区分")
    done("summary", "两代谱系按强/弱/无分层，字节身份与来源声明界限清晰")


def main():
    parser = argparse.ArgumentParser(description="两代合流成果的分支谱系示例")
    parser.add_argument("step", nargs="?", default="summary", choices=STEPS)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pagefold-lineage-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                run(Client(server.server_port), args.step)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
