"""引用见证入口的独立工作流检查。

构造两份同模式、同主键但见证行不同的镜像（含复合外键、半截 NULL 键、
外键循环和超过单组展开上限的大量入向引用），走真实 HTTP 入口验证：

- 上游复合外键与下游入向引用都能找到实际见证；
- 两列中一列 NULL 即无引用，并在页面/JSON 中交代；
- 循环被标记而不是无限展开；
- 大量入向引用按上限展开并报告未展开数量；
- 相同主键在哪份镜像查询，就只得到那份镜像的见证（镜像隔离）。

运行：python3 -m examples.witness_check
"""

import json
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

KEY_NORTH_1 = json.dumps([["t", "north"], ["i", 1]])
KEY_ITEM_NULL = json.dumps([["i", 2002]])
KEY_HUB_1 = json.dumps([["i", 1]])


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)
    print("  ✓ " + description)


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None, raw=False):
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
        expect(actual == status, f"{route}: 期望 {status}，实际 {actual}：{body[:300]!r}")
        if raw:
            return body
        return json.loads(body) if kind == "application/json" else body

    def upload(self, name):
        return self.call("/api/images", image(name), 201, {
            "X-Image-Name": name, "Content-Type": "application/octet-stream"})


def q(params):
    return urlencode(params)


def pair_witness(client, image_id, **extra):
    params = {"table": "w_pair", "key": KEY_NORTH_1}
    params.update(extra)
    return client.call(f"/api/images/{image_id}/witness?" + q(params))


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-witness-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            client = Client(server.server_port)
            try:
                run(client)
            finally:
                server.shutdown()
                thread.join()
    print("witness check ✓ 全部通过")


def run(client):
    print("[1] 入口与登记")
    home = client.call("/", raw=True).decode()
    expect('href="/witness">引用见证' in home, "首页/导航提供引用见证入口")
    entry = client.call("/witness", raw=True).decode()
    expect("指定镜像与主键" in entry and "上下游" in entry, "/witness 渲染查询表单与说明")
    alpha = client.upload("witness-alpha")
    beta = client.upload("witness-beta")
    aid, bid = alpha["id"], beta["id"]
    expect(aid != bid, "两份对照镜像摘要不同")

    print("[2] 上游复合外键见证")
    result = pair_witness(client, aid)
    expect(result["image"]["id"] == aid, "响应标明见证只来自 alpha 镜像")
    expect(result["root"]["values"][2] == ["t", "alpha-pair-one"], "起点行是 alpha 的 north/1")
    up = result["upstream"]
    expect(len(up) == 1 and up[0]["status"] == "matched", "north/1 的复合外键找到上游行")
    expect(up[0]["constraint"]["from_columns"] == ["next_tenant", "next_seq"],
           "边来源保留复合外键列次序")
    expect(up[0]["row"][2] == ["t", "alpha-pair-two"], "上游见证是 alpha 的 north/2")

    print("[3] 下游入向引用与大量行截断")
    groups = {g["table"]: g for g in result["downstream"]}
    expect("w_item" in groups and "w_pair" in groups, "下游同时列出入向子表与自引用")
    items = groups["w_item"]
    expect(items["total"] == 65 and items["shown"] == 50 and items["omitted"] == 15,
           "65 行入向引用默认展开 50，精确报告省略 15")
    expect(items["truncated"] is True and "未展开" in items["explain"], "截断组交代未展开部分")
    labels = [row[3][1] for row in items["rows"]]
    expect("alpha-child-000" in labels and "alpha-child-049" in labels, "前 50 个实际见证在列")
    expect("alpha-child-050" not in labels, "第 51 行起没有冒充已展开")
    full = pair_witness(client, aid, limit=200)
    full_items = next(g for g in full["downstream"] if g["table"] == "w_item")
    expect(full_items["shown"] == 65 and full_items["omitted"] == 0
           and full_items["rows"][-1][3][1] == "alpha-child-064",
           "提高 limit 后可展开全部 65 行")

    print("[4] 外键循环被标记且不会无限展开")
    expect(len(result["cycles"]) >= 1, "复合键自引用循环被检测到")
    route = result["cycles"][0]["route"]
    expect("w_pair[north/1]" in route and "w_pair[north/2]" in route, f"循环路径含两端键：{route}")
    hub = client.call(f"/api/images/{aid}/witness?" + q({"table": "w_hub", "key": KEY_HUB_1}))
    expect(any("w_hub[1]" in c["route"] and "w_hub[2]" in c["route"] for c in hub["cycles"]),
           "单键自引用环 1↔2 被标记")
    expect(len(hub["nodes"]) < 240, "循环没有造成节点无限增长")

    print("[5] 复合外键一列 NULL 即无引用")
    null_case = client.call(f"/api/images/{aid}/witness?"
                            + q({"table": "w_item", "key": KEY_ITEM_NULL}))
    null_up = next(g for g in null_case["upstream"]
                   if g["constraint"]["from_columns"] == ["tenant", "seq"])
    expect(null_up["status"] == "contains_null" and null_up["matched"] is False,
           "含 NULL 列的外键不生成匹配边")
    expect(null_up["null_columns"] == ["seq"], "明确指出为 NULL 的是 seq 列")
    expect(null_up["witness"] is None and null_up["row"] is None, "不会退化成只按 tenant 匹配")
    item_group = next(g for g in result["downstream"] if g["table"] == "w_item")
    expect(item_group.get("null_like", 0) >= 1,
           "父行下游额外交代含 NULL 的半截键未被计为引用")
    expect("null_explain" in item_group, "JSON 给出 NULL 近邻说明文本")
    null_html = client.call(f"/images/{aid}/witness?"
                            + q({"table": "w_item", "key": KEY_ITEM_NULL}), raw=True).decode()
    expect("MATCH SIMPLE" in null_html and "seq 为 NULL" in null_html,
           "HTML 交代 NULL 无引用规则与责任列")

    print("[6] 相同键的镜像隔离证明")
    alpha_text = json.dumps(pair_witness(client, aid), ensure_ascii=False)
    beta_result = pair_witness(client, bid)
    beta_text = json.dumps(beta_result, ensure_ascii=False)
    expect("alpha-child-000" in alpha_text and "beta-child-000" not in alpha_text,
           "alpha 镜像的见证不含 beta 数据")
    expect("beta-child-000" in beta_text and "alpha-child-000" not in beta_text,
           "相同 north/1 键在 beta 上只见 beta 见证")
    beta_items = next(g for g in beta_result["downstream"] if g["table"] == "w_item")
    expect(beta_items["total"] == 3, "beta 上同一键只有 3 行入向引用")
    alpha_html = client.call(f"/images/{aid}/witness?"
                             + q({"table": "w_pair", "key": KEY_NORTH_1}), raw=True).decode()
    beta_html = client.call(f"/images/{bid}/witness?"
                            + q({"table": "w_pair", "key": KEY_NORTH_1}), raw=True).decode()
    expect("alpha-child-000" in alpha_html and "beta-child-000" not in alpha_html,
           "HTML 同样按镜像隔离")
    expect("beta-child-000" in beta_html and "alpha-child-000" not in beta_html,
           "beta HTML 不含 alpha 见证")

    print("[7] 页面交代边来源与未展开部分")
    expect("边来源" in alpha_html and "w_item" in alpha_html and "tenant" in alpha_html,
           "每条边标注外键约束、表与列")
    expect("另有 15 行未展开" in alpha_html, "页面写出未展开数量")
    expect("未计为见证" in alpha_html, "父行页面说明含 NULL 的半截键为何缺席")
    expect("外键循环" in alpha_html, "页面有循环专区")
    expect("跨镜像对照" in alpha_html and "/witness?image=" in alpha_html,
           "页面提供其他镜像同键对照入口")
    rows_page = client.call(f"/images/{aid}/rows?table=w_pair", raw=True).decode()
    expect("上下游见证 ↗" in rows_page, "行窗口每一行提供见证深链")

    print("[7b] 展开出的每个节点页都能渲染（含无上游、只有下游的表）")
    for node in result["nodes"]:
        if node["exists"]:
            client.call(node["url"], raw=True)
    quartz = client.upload("quartz")
    qid = quartz["id"]
    bench = client.call(f"/api/images/{qid}/witness?"
                        + q({"table": "bench", "key": json.dumps([["t", "north"]])}))
    expect(any(g["table"] == "node" and g["total"] >= 1 for g in bench["downstream"]),
           "bench 这类自身无外键、只有入向引用的表也能统计下游")
    bench_html = client.call(f"/images/{qid}/witness?"
                             + q({"table": "bench", "key": json.dumps([["t", "north"]])}),
                             raw=True).decode()
    expect("edge-card" in bench_html and "node" in bench_html, "bench 见证页正常渲染边卡片")
    for node in bench["nodes"]:
        if node["exists"]:
            client.call(node["url"], raw=True)

    print("[8] 入口别名、参数兼容与错误码")
    top = client.call("/api/witness?" + q({"image": aid, "table": "w_pair", "key": KEY_NORTH_1}))
    expect(top["image"]["id"] == aid, "顶层 /api/witness?image= 可用")
    colparams = client.call(f"/api/images/{aid}/witness?table=w_pair&tenant=north&seq=1")
    expect(colparams["root"]["key"] == [["t", "north"], ["i", "1"]], "支持按主键列名直接传值")
    client.call(f"/api/images/{aid}/witness?table=w_pair", status=400)
    missing_table = client.call(
        f"/api/images/{aid}/witness?" + q({"table": "nope", "key": "[[i,1]]"}), status=404)
    expect(missing_table["code"] == "unknown_table", "未知表返回 404 unknown_table")
    missing_row = client.call(f"/api/images/{aid}/witness?" + q(
        {"table": "w_pair", "key": json.dumps([["t", "zzz"], ["i", 1]])}), status=404)
    expect(missing_row["code"] == "unknown_row", "镜像中无此主键行返回 404 unknown_row")
    client.call("/witness?image=" + aid + "&table=w_pair&step=columns", raw=True)


if __name__ == "__main__":
    main()
