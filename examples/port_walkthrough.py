"""分支变更移植的端到端工作流检查（真实 HTTP 入口）。

覆盖：
- 对照移植前的三个数据库（上轮祖本 / 改动分支 / 新祖本）建立移植计划；
- 逐行处置与来源（自动移植 / 已提前到位 / 保留 / 重新选择 / 待决）；
- 复合主键改名组的完整移植、保留未改名、部分受阻三种报告；
- 保留与重新选择两条路径的重演，以及外键拦截后的独立输出核验。
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

from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

WORKFLOWS = ("import", "plan", "review", "decide", "keep", "reselect", "blocked", "download")


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, headers=None):
        headers = dict(headers or {})
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

    def upload(self, name, status=201):
        return self.call("/api/images", image(name), status,
                         {"Content-Type": "application/octet-stream", "X-Image-Name": name})

    def page(self, route):
        return self.call(route)


def node_notes(candidate):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        notes = {f"{bench}/{slot}": note for bench, slot, note in
                 db.execute("SELECT bench,slot,note FROM node ORDER BY bench,slot")}
        bindings = {row[0]: (row[1], row[2]) for row in
                    db.execute("SELECT id,bench,slot FROM binding ORDER BY id")}
        fk = db.execute("PRAGMA foreign_key_check").fetchall()
        return notes, bindings, fk
    finally:
        db.close()


def find_item(port, table, key):
    return next(item for item in port["items"] if item["table"] == table and item["key"] == key)


def decide(client, route, revision, item_id, choice, status=200):
    return client.call(route + "/decisions", {"revision": revision, "item": item_id,
                                              "choice": choice}, status)


def run(client, goal):
    def done(workflow, result):
        if goal in (workflow, "all"):
            print(f"{workflow} ✓ {result}")
        return goal == workflow

    home = client.call("/")
    expect("分支变更移植".encode() in home and b"/demo-port" in home, "首页缺少移植入口")
    ids = {name: client.upload("port-" + name)["id"]
           for name in ("base", "branch", "target")}
    blocked_id = client.upload("port-blocked-target")["id"]
    if done("import", "上轮祖本、改动分支、新祖本与外键受阻镜像分别登记"):
        return
    sources = {"base": ids["base"], "left": ids["branch"], "right": ids["target"]}
    port = client.call("/api/ports", sources, 201)
    route = f"/api/ports/{port['id']}"
    expect(port["unresolved"] == 2 and len(port["items"]) == 5, "移植条目数量不符")
    labels = {(i["table"], json.dumps(i["key"])): i["disposition_label"] for i in port["items"]}
    expect(labels[("binding", json.dumps([["i", "2"]]))] == "自动移植", "引用改指未识别为自动移植")
    expect(labels[("node", json.dumps([["t", "south"], ["i", "3"]]))] == "已提前到位",
           "提前到位行未识别")
    expect(len(port["groups"]) == 1 and port["groups"][0]["status"] == "pending",
           "复合主键改名未识别为待决改名组")
    if done("plan", "自动项、提前到位项与改名组按三库对照生成"):
        return
    page = client.page(f"/ports/{port['id']}")
    expect("逐行处置与来源".encode() in page and "复合主键改名组".encode() in page,
           "移植页未交代处置、来源与改名组")
    expect(client.call(route + "/rehearse", {"revision": 0}, 409)["code"] == "unresolved",
           "未决移植未拦住重演")
    expect(client.call("/api/ports/missing", status=404)["code"] == "unknown_port", "未知移植未拒绝")
    if done("review", "改名组待决时禁止重演，页面逐行交代来源"):
        return
    east = find_item(port, "node", [["t", "east"], ["i", "1"]])
    west_old = find_item(port, "node", [["t", "west"], ["i", "1"]])
    expect(east["disposition"] == "pending" and west_old["disposition"] == "pending",
           "冲突行未处于待裁决")
    stale = decide(client, route, 9, east["id"], "right", 409)
    expect(stale["code"] == "stale_revision", "过期版本未拒绝")
    first = decide(client, route, 0, east["id"], "right")
    expect(first["revision"] == 1, "首次裁决未推进版本")
    again = decide(client, route, 1, east["id"], "right")
    expect(again["revision"] == 1, "同选择重复裁决修改了版本")
    if done("decide", "保留/重新选择裁决带版本，过期拒绝且同值幂等"):
        return

    # 路径一：保留新祖本（两处冲突都选 right）
    keep_port = client.call("/api/ports", sources, 201)
    keep_route = f"/api/ports/{keep_port['id']}"
    rev = 0
    for item_id in (find_item(keep_port, "node", [["t", "east"], ["i", "1"]])["id"],
                    find_item(keep_port, "node", [["t", "west"], ["i", "1"]])["id"]):
        rev = decide(client, keep_route, rev, item_id, "right")["revision"]
    keep_res = client.call(keep_route + "/rehearse", {"revision": rev})
    expect(keep_res["status"] == "passed", "保留路径重演未通过")
    expect(keep_res["rename_groups"][0]["status"] == "retained", "保留路径误报完整改名")
    expect(keep_res["rename_complete"] is False and keep_res["applied"] == 1
           and keep_res["skipped"] == 4, "保留路径的实际写入/跳过统计不符")
    if done("keep", "保留新祖本：改名组报保留未改名，仅引用改指真正写入"):
        return

    # 路径二：重新选择改动分支（两处冲突都选 left）
    sel_port = client.call("/api/ports", sources, 201)
    sel_route = f"/api/ports/{sel_port['id']}"
    rev = 0
    for item_id in (find_item(sel_port, "node", [["t", "east"], ["i", "1"]])["id"],
                    find_item(sel_port, "node", [["t", "west"], ["i", "1"]])["id"]):
        rev = decide(client, sel_route, rev, item_id, "left")["revision"]
    sel_res = client.call(sel_route + "/rehearse", {"revision": rev})
    expect(sel_res["status"] == "passed", "重新选择路径重演未通过")
    expect(sel_res["rename_groups"][0]["status"] == "complete", "删旧加新两半齐备却未报完整移植")
    expect(sel_res["rename_complete"] is True, "完整改名标志不符")
    if done("reselect", "重新选择改动分支：复合主键改名完整移植并通过校验"):
        return

    # 外键受阻：新祖本多一条仍指向旧键的引用，完整改名删旧键时被拦截
    blocked_sources = dict(sources, right=blocked_id)
    bad_port = client.call("/api/ports", blocked_sources, 201)
    bad_route = f"/api/ports/{bad_port['id']}"
    rev = 0
    for item_id in (find_item(bad_port, "node", [["t", "east"], ["i", "1"]])["id"],
                    find_item(bad_port, "node", [["t", "west"], ["i", "1"]])["id"]):
        rev = decide(client, bad_route, rev, item_id, "left")["revision"]
    bad_res = client.call(bad_route + "/rehearse", {"revision": rev})
    expect(bad_res["status"] == "blocked" and bad_res["problem"]["code"] == "foreign_key",
           "外键拦截未上报")
    expect(bad_res["rename_groups"][0]["status"] == "blocked"
           and bad_res["rename_groups"][0]["blocked_by"] == "foreign_key",
           "受阻改名未降级，存在把半套动作报成完整移植的风险")
    expect(client.call(bad_route + "/download", status=404)["code"] == "unpublished",
           "受阻移植竟然可下载候选")
    history = client.call(bad_route + "/attempts")
    expect(history[0]["status"] == "blocked", "受阻重演未独立留痕")
    if done("blocked", "外键冲突拦截改名，改名组报受阻且不产生可下载候选"):
        return

    # 独立输出核验：下载两个通过候选，对照三库检查实际行与外键
    keep_blob = client.call(keep_route + "/download")
    sel_blob = client.call(sel_route + "/download")
    expect(hashlib.sha256(keep_blob).hexdigest() == keep_res["candidate_digest"], "保留候选摘要不符")
    expect(hashlib.sha256(sel_blob).hexdigest() == sel_res["candidate_digest"], "重选候选摘要不符")
    keep_notes, keep_bind, keep_fk = node_notes(keep_blob)
    sel_notes, sel_bind, sel_fk = node_notes(sel_blob)
    expect(keep_fk == [] and sel_fk == [], "候选存在外键违规")
    # 保留路径：旧键仍在、取本轮业务值；引用已随自动移植改指新键
    expect(keep_notes["west/1"] == "本轮保留旧键" and "west/2" in keep_notes, "保留路径旧键丢失")
    expect(keep_notes["east/1"] == "本轮业务改值", "保留路径未保留本轮业务值")
    expect(keep_bind[2] == ("west", 2), "引用未随分支改指新键")
    # 重选路径：旧键消失、新键承接，east 取分支值，引用指向新键
    expect("west/1" not in sel_notes and sel_notes["west/2"] == "待改名节点", "重选路径改名不完整")
    expect(sel_notes["east/1"] == "分支更新值", "重选路径未采纳分支值")
    expect(sel_bind[2] == ("west", 2), "重选路径引用指向错误")
    # 提前到位的新增行两条路径都在，且候选可独立打开
    expect(sel_notes["south/3"] == "分支新增节点" and keep_notes["south/3"] == "分支新增节点",
           "提前到位行在候选中丢失")
    done("download", "两份独立候选摘要、行内容与外键分别核验通过")


def main():
    parser = argparse.ArgumentParser(description="验证 Pagefold 分支移植工作流")
    parser.add_argument("workflow", choices=("all", *WORKFLOWS))
    with tempfile.TemporaryDirectory(prefix="pagefold-port-") as root:
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
