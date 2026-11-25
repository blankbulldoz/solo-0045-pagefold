"""创建计划前的可确认预览：端到端工作流检查。

覆盖任务要求的四组事实：
1. 无冲突、普通冲突、大变更量三类实例，预览数字与实际落库计划逐项一致；
2. 同标签不同字节的镜像互换后，绑定校验阻止错误来源写入；
3. 两个窗口（含真实并发）确认同一预览，只产生同一个正式计划；
4. 只预览不确认（中途退出）不会在 plans 表留下记录。

运行：python3 -m examples.preview_flow
"""

import json
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

BIG_TABLE = "CREATE TABLE item(id INTEGER NOT NULL PRIMARY KEY, label TEXT NOT NULL)"
PLAN_LIMIT_ITEMS = 200
PLAN_LIMIT_CONFLICTS = 50


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


def clean_bytes(side):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE tidy(id INTEGER NOT NULL PRIMARY KEY, label TEXT NOT NULL)")
        db.executemany("INSERT INTO tidy VALUES(?,?)",
                       [(i, f"base-{i}") for i in range(10)])
        if side == "left":
            db.execute("UPDATE tidy SET label='left-only' WHERE id=1")
            db.executemany("INSERT INTO tidy VALUES(?,?)", [(100, "new-a"), (101, "new-b")])
        elif side == "right":
            db.execute("UPDATE tidy SET label='right-only' WHERE id=2")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def big_bytes(side):
    db = sqlite3.connect(":memory:")
    try:
        db.execute(BIG_TABLE)
        db.executemany("INSERT INTO item VALUES(?,?)", [(i, f"base-{i}") for i in range(300)])
        if side == "left":
            db.executemany("UPDATE item SET label=? WHERE id=?",
                           [(f"left-{i}", i) for i in range(220)])
            db.executemany("INSERT INTO item VALUES(?,?)",
                           [(i, f"added-{i}") for i in range(10000, 10060)])
            db.execute("DELETE FROM item WHERE id BETWEEN 260 AND 299")
        elif side == "right":
            db.executemany("UPDATE item SET label=? WHERE id=?",
                           [(f"right-{i}", i) for i in range(100, 200)])
        db.commit()
        return db.serialize()
    finally:
        db.close()


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def raw(self, route, data=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if data is not None:
            if isinstance(data, (dict, list)) and "Content-Type" not in request_headers:
                body = json.dumps(data).encode()
                request_headers["Content-Type"] = "application/json"
            elif isinstance(data, bytes):
                body = data
            else:
                body = urlencode(data).encode()
                request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(self.origin + route, data=body, headers=request_headers)
        try:
            with urlopen(request, timeout=15) as result:
                return result.status, dict(result.headers), result.read(), result.url
        except HTTPError as error:
            with error:
                return error.code, dict(error.headers), error.read(), error.url

    def call(self, route, data=None, status=200, headers=None):
        actual, response_headers, body, _url = self.raw(route, data, headers)
        expect(actual == status, f"{route}: {actual} != {status}: {body[:400]!r}")
        kind = response_headers.get("Content-Type", "")
        return json.loads(body) if "application/json" in kind else body

    def upload(self, label, blob):
        return self.call("/api/images", blob, 201, {
            "X-Image-Name": quote(label), "Content-Type": "application/octet-stream"})

    def plans_count(self):
        return len(self.call("/api/plans"))


def strip_choice(items):
    return [{key: value for key, value in item.items() if key != "choice"} for item in items]


def check_preview_matches_plan(client, sources, expected=None):
    preview = client.call("/api/plans/preview", sources)
    expect(len(preview["preview_token"]) == 64, "预览凭证缺失")
    expect(preview["sources"] == sources, "预览来源摘要与请求不一致")
    for role in ("base", "left", "right"):
        record = preview["roles"][role]
        expect(record["id"] == sources[role] and record["role"] == role, "角色卡绑定错误")
        expect(isinstance(record["label"], str) and record["size"] > 0, "角色卡信息不全")
    if expected:
        for key, value in expected.items():
            got = preview
            for part in key.split("."):
                got = got[part]
            expect(got == value, f"预览指标 {key}={got!r}，期望 {value!r}")
    plan = client.call("/api/plans", dict(sources, preview_token=preview["preview_token"]), 201)
    full = client.call(f"/api/plans/{plan['id']}")
    expect(full["sources"] == sources, "正式计划写错来源")
    expect(full["plan_digest"] == preview["plan_digest"], "正式计划摘要与预览不一致")
    expect(len(full["items"]) == preview["total"], "正式计划项数与预览不一致")
    expect(full["unresolved"] == preview["estimate"]["conflicts"], "未决数与预览冲突数不一致")
    expect(strip_choice(full["items"]) == preview["items"], "落库计划项与预览逐项不一致")
    expect(plan["id"] == full["id"], "确认返回的计划无法回读")
    status = client.call(f"/api/plans/preview/{preview['preview_token']}")
    expect(status["confirmed"] is True and status["plan_id"] == plan["id"], "预览确认状态未回写")
    return preview, full


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-preview-") as root:
        state_path = Path(root) / "state.sqlite"
        with make_server(Store(state_path), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            client = Client(server.server_port)
            try:
                # ---- 实例一：无冲突（仅单边改动） ----
                base = client.upload("clean-base", clean_bytes("base"))
                left = client.upload("clean-left", clean_bytes("left"))
                right = client.upload("clean-right", clean_bytes("right"))
                clean_sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
                before = client.plans_count()
                preview, plan = check_preview_matches_plan(client, clean_sources, {
                    "total": 4, "estimate.conflicts": 0, "resolution.conflict": 0,
                    "change_types.added": 2, "change_types.modified": 2,
                    "scale.large": False})
                expect(client.plans_count() == before + 1, "无冲突实例未恰好创建一个计划")
                print("clean ✓ 无冲突实例：预览 4 项差异、0 冲突，与落库计划逐项一致")

                # 三库完全相同：0 差异也应成立
                same = client.upload("same", clean_bytes("base"))
                zero_sources = {"base": same["id"], "left": same["id"], "right": same["id"]}
                zp, zplan = check_preview_matches_plan(client, zero_sources, {"total": 0})
                print("identical ✓ 零差异实例预览与空计划一致")

                # ---- 实例二：普通冲突（walkthrough 同款素材） ----
                q = client.upload("quartz", image("quartz"))
                c = client.upload("cerulean", image("cerulean"))
                u = client.upload("umber", image("umber"))
                normal_sources = {"base": q["id"], "left": c["id"], "right": u["id"]}
                preview, plan = check_preview_matches_plan(client, normal_sources, {
                    "total": 3, "estimate.conflicts": 1, "scale.large": False})
                html_status, _h, html, _url = client.raw("/plans/preview", normal_sources)
                page = html.decode("utf-8")
                expect(html_status == 200, "HTML 预览页状态不对")
                for text in ("创建前预览", "选定角色", "变更类型", "预估处理量",
                             "处于可直接审阅规模", "preview_token", q["id"], c["id"], u["id"]):
                    expect(text in page, f"HTML 预览缺少内容：{text}")
                print("normal ✓ 普通冲突实例：3 项差异、1 冲突，JSON/HTML 预览与计划一致")

                # ---- 实例三：大变更量 ----
                bbig = client.upload("big-base", big_bytes("base"))
                lbig = client.upload("big-left", big_bytes("left"))
                rbig = client.upload("big-right", big_bytes("right"))
                big_sources = {"base": bbig["id"], "left": lbig["id"], "right": rbig["id"]}
                preview, plan = check_preview_matches_plan(client, big_sources, {
                    "total": 320, "estimate.conflicts": 100,
                    "change_types.added": 60, "change_types.deleted": 40,
                    "change_types.modified": 220,
                    "estimate.estimated_row_writes": 220,
                    "scale.large": True, "scale.level": "large",
                    "scale.direct_review_limit": PLAN_LIMIT_ITEMS,
                    "scale.direct_conflict_limit": PLAN_LIMIT_CONFLICTS})
                expect("reduce_scope" in preview["actions"], "大规模预览未给出缩小范围动作")
                expect({"confirm", "revise", "reduce_scope"} <= set(preview["actions"]),
                       "大规模预览动作不完整")
                _s, _h, big_html, _u = client.raw("/plans/preview", big_sources)
                big_page = big_html.decode("utf-8")
                for text in ("超过直接审阅规模", "缩小范围", "仍要创建正式计划",
                             'data-large="true"'):
                    expect(text in big_page, f"大规模 HTML 预览缺少：{text}")
                print("large ✓ 大变更量实例：320 项/100 冲突触发超限动作，预览与计划一致")

                # ---- 中途退出：只预览不确认，不产生计划记录 ----
                count_before = client.plans_count()
                db_before = sqlite3.connect(state_path).execute(
                    "SELECT COUNT(*) FROM plans").fetchone()[0]
                exit_preview = client.call("/api/plans/preview", normal_sources)
                expect(client.call(f"/api/plans/preview/{exit_preview['preview_token']}")[
                           "confirmed"] is False, "新预览不应是已确认状态")
                client.raw("/plans/preview", big_sources)  # HTML 预览后直接离开
                expect(client.plans_count() == count_before, "中途退出后 API 计划列表增加")
                db_after = sqlite3.connect(state_path).execute(
                    "SELECT COUNT(*) FROM plans").fetchone()[0]
                expect(db_after == db_before, "中途退出后 plans 表出现记录")
                print("cancel ✓ 中途退出：API 与 plans 表均无新增计划记录")

                # ---- 同标签、不同字节：互换来源必须被绑定校验拦住 ----
                def mirror_bytes(marker):
                    memory = sqlite3.connect(":memory:")
                    try:
                        memory.deserialize(image("quartz"))
                        memory.execute("INSERT INTO bench VALUES(?,?)", (f"mirror-{marker}", marker))
                        memory.commit()
                        return memory.serialize()
                    finally:
                        memory.close()

                dup_a = client.upload("重复标签", mirror_bytes("甲"))
                dup_b = client.upload("重复标签", mirror_bytes("乙"))
                expect(dup_a["id"] != dup_b["id"], "不同字节应得到不同摘要")
                expect(client.call(f"/api/images/{dup_a['id']}")["label"] == "重复标签",
                       "同标签镜像 A 标签未保存")
                swap_sources = {"base": q["id"], "left": dup_a["id"], "right": dup_b["id"]}
                sp = client.call("/api/plans/preview", swap_sources)
                expect("重复标签" in sp["repeated_labels"], "未识别同标签异字节")
                _st, _hh, swap_preview_html, _uu = client.raw("/plans/preview", swap_sources)
                expect("标签重复但字节不同" in swap_preview_html.decode(),
                       "HTML 预览未警示同标签异字节")
                count_before = client.plans_count()
                swapped = dict(swap_sources, left=dup_b["id"],
                               preview_token=sp["preview_token"])
                status, _h, body, _u = client.raw("/api/plans", swapped)
                problem = json.loads(body)
                expect(status == 409 and problem["code"] == "preview_binding",
                       f"互换来源未被拒绝：{status} {body[:200]!r}")
                expect(client.plans_count() == count_before, "绑定冲突时仍创建了计划")
                # 不带来源、只持凭证：以服务端绑定为准，落库的是原来源
                bound = client.call("/api/plans",
                                    {"preview_token": sp["preview_token"]}, 201)
                expect(bound["sources"] == swap_sources, "凭证确认写错来源")
                expect(bound["sources"]["left"] == dup_a["id"] != dup_b["id"],
                       "互换后的镜像被写入正式计划")
                again = client.call("/api/plans",
                                    dict(swap_sources, preview_token=sp["preview_token"]), 201)
                expect(again["id"] == bound["id"] and again.get("already_confirmed"),
                       "重复确认同一预览未幂等返回同一计划")
                print("swap ✓ 同标签异字节互换被拒；凭证绑定来源不被调包，重复确认幂等")

                # ---- 两个窗口并发确认同一预览：同一个正式计划 ----
                twin = client.call("/api/plans/preview", normal_sources)
                token = twin["preview_token"]
                window_one = client.call(f"/plans/preview/{token}")
                window_two = client.call(f"/plans/preview/{token}")
                expect(isinstance(window_one, bytes) and token.encode() in window_one,
                       "预览固定链接未渲染确认页")
                expect(window_one == window_two, "两个窗口打开的同一预览不一致")
                payload = dict(normal_sources, preview_token=token)
                barrier = Barrier(2)

                def confirm_in_window(_index):
                    barrier.wait(timeout=5)
                    return client.raw("/api/plans", payload)

                plans_before = client.plans_count()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(confirm_in_window, (0, 1)))
                ids = []
                for status, _h, body, _u in responses:
                    expect(status == 201, f"并发确认状态异常：{status} {body[:200]!r}")
                    ids.append(json.loads(body)["id"])
                expect(ids[0] == ids[1], "两个窗口确认同一预览产生了不同计划")
                expect(client.plans_count() == plans_before + 1,
                       "并发确认产生了多于一个计划")
                print("windows ✓ 两窗口并发确认同一预览：只落一个且来源正确的正式计划")

                # ---- 网页表单完整链路：预览 PRG -> 303 -> 预览页 -> 303 -> 计划页 ----
                import http.client
                import re
                encoded_preview = urlencode(normal_sources)
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("POST", "/plans/preview", body=encoded_preview, headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(encoded_preview.encode()))})
                response = connection.getresponse()
                response.read()
                expect(response.status == 303, f"预览表单状态 {response.status}")
                preview_location = response.getheader("Location")
                connection.close()
                expect(preview_location and preview_location.startswith("/plans/preview/"),
                       "预览未重定向到绑定凭证的固定链接")
                form_html_status, _h, form_body, _u = client.raw(preview_location)
                expect(form_html_status == 200, "预览固定链接状态不对")
                form_page = form_body.decode()
                m_token = re.search(r'name="preview_token" value="([0-9a-f]{64})"', form_page)
                expect(m_token, "HTML 预览未包含确认凭证")
                expect(m_token.group(1) in preview_location, "预览链接凭证与表单凭证不一致")
                form = {"preview_token": m_token.group(1)}
                for role in ("base", "left", "right"):
                    m = re.search(f'name="{role}" value="([0-9a-f]{{64}})"', form_page)
                    expect(m, f"HTML 预览缺少 {role} 绑定字段")
                    form[role] = m.group(1)
                encoded = urlencode(form)
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                connection.request("POST", "/plans", body=encoded, headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(encoded.encode()))})
                response = connection.getresponse()
                response.read()
                expect(response.status == 303, f"表单确认状态 {response.status}")
                location = response.getheader("Location")
                connection.close()
                expect(location and location.startswith("/plans/"), "重定向未指向正式计划")
                _s, _h, final_body, final_url = client.raw(location)
                final_page = final_body.decode()
                expect("主键行差异" in final_page and "1 项待裁决" in final_page,
                       "确认后的计划页内容不符")
                print("html ✓ 网页表单：预览 PRG→确认 303→正式计划页全链路通过")
                print("\n全部预览确认检查通过。")
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
