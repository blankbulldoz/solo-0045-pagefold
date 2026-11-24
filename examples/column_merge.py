"""列合成工作流验证：通过真实 HTTP 入口演示整行裁决与逐列合成。

覆盖：
  combine      node ('north',1) 左支改备注、右支改 BLOB/权重 —— 整行裁决与列合成各演示一次
  conflict     同一列在两个分支各有不同新值 —— 必须逐列显式选边
  deletion     删除碰上修改 —— 禁止列合成，只能整行裁决
  constraint   独立列拼出的行违反表级 CHECK —— 重演受阻并保留处理路径
"""
import argparse
import json
import sqlite3
import tempfile
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pagefold.server import make_server
from pagefold.store import Store

NODE_SCHEMA = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    note TEXT, payload BLOB, weight REAL CHECK(weight >= 0),
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""

PAIR_SCHEMA = """
CREATE TABLE pair(
    id TEXT PRIMARY KEY, span INTEGER NOT NULL, weight REAL NOT NULL,
    CHECK(span + weight <= 10)
) WITHOUT ROWID;
"""

def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200, form=False):
        headers = {}
        if data is None:
            body = None
        elif form:
            body = urlencode(data, encoding="utf-8").encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=body, headers=headers)
        try:
            with urlopen(request, timeout=10) as result:
                raw, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as exc:
            with exc:
                raw, actual, kind = exc.read(), exc.code, exc.headers.get_content_type()
        expect(actual == status, f"{route}: {actual} != {status}: {raw[:300]}")
        return json.loads(raw) if kind == "application/json" else raw

    def page(self, route):
        return self.call(route).decode("utf-8")

    def upload(self, label, data):
        request = Request(self.origin + "/api/images", data=data, headers={
            "X-Image-Name": label, "Content-Type": "application/octet-stream"})
        with urlopen(request, timeout=10) as result:
            return json.loads(result.read())


def build_image(schema, statements, mutate=None):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema)
        for statement, rows in statements:
            if rows:
                db.executemany(statement, rows)
            else:
                db.execute(statement)
        if mutate:
            mutate(db)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def node_seed():
    return [
        ("INSERT INTO bench VALUES(?,?)", [("north", "玄石"), ("south", "砂层")]),
        ("INSERT INTO node VALUES(?,?,?,?,?)",
         [("north", 1, "祖本节点", b"\x00\x14", 1.5), ("south", 1, "稳定节点", b"", 3.0)]),
        ("INSERT INTO binding VALUES(1,'north',1)", []),
    ]


def make_node_variant(flavor):
    def mutate(db):
        if flavor == "left":
            db.execute("UPDATE node SET note='左支备注' WHERE bench='north'")
        elif flavor == "right":
            db.execute("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'")
        elif flavor == "conflict-left":
            db.execute("UPDATE node SET note='左支备注',weight=4.0 WHERE bench='north'")
        elif flavor == "conflict-right":
            db.execute("UPDATE node SET note='右支备注',payload=x'ab' WHERE bench='north'")
        elif flavor == "delete-left":
            db.execute("DELETE FROM binding WHERE bench='north' AND slot=1")
            db.execute("DELETE FROM node WHERE bench='north' AND slot=1")
        elif flavor == "delete-right":
            db.execute("UPDATE node SET note='右支备注' WHERE bench='north'")
    return build_image(NODE_SCHEMA, node_seed(), mutate if flavor else None)

def make_pair_variant(flavor):
    # 祖本 (3, 4) 合计 7；左支 (3, 6)、右支 (6, 4) 各自满足 <=10，
    # 但把左支的 weight 与右支的 span 合成得到 (6, 6) 合计 12，违反行级约束。
    statements = [("INSERT INTO pair VALUES(?,?,?)", [("k1", 3, 4.0)])]

    def mutate(db):
        if flavor == "left":
            db.execute("UPDATE pair SET weight=6.0 WHERE id='k1'")
        elif flavor == "right":
            db.execute("UPDATE pair SET span=6 WHERE id='k1'")
    return build_image(PAIR_SCHEMA, statements, mutate if flavor else None)


def conflict_item(plan):
    return next(item for item in plan["items"] if item["automatic"] is None)


def decide(client, route, revision, item_id, choice, columns=None, form=False, status=200):
    payload = {"revision": revision, "item": item_id, "choice": choice}
    if columns is not None:
        if form:
            for name, role in columns.items():
                payload[f"col.{name}"] = role
        else:
            payload["columns"] = columns
    return client.call(route + "/decisions", payload, status, form=form)


def rehearse(client, route, revision):
    return client.call(route + "/rehearse", {"revision": revision})


def open_candidate(client, route):
    data = client.call(route + "/download")
    db = sqlite3.connect(":memory:")
    db.deserialize(data)
    return db


def scenario_combine(client):
    base = client.upload("node-base", make_node_variant(None))
    left = client.upload("node-left", make_node_variant("left"))
    right = client.upload("node-right", make_node_variant("right"))
    plan = client.call("/api/plans", {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)
    route = f"/api/plans/{plan['id']}"
    item = conflict_item(plan)
    expect(item["merge"]["combinable"] is True, "独立列改动应被识别为可以组合")
    expect({c["name"]: c["source"] for c in item["merge"]["columns"] if c["status"] == "side"}
           == {"note": "left", "payload": "right", "weight": "right"}, "逐列来源建议不正确")
    html = client.page(f"/plans/{plan['id']}")
    expect("可以组合" in html and "已经采纳" not in html, "页面应先提示可以组合且未采纳")
    expect("note ← 左支" in html and "payload ← 右支" in html and "weight ← 右支" in html,
           "页面未展示逐列来源建议")

    # 第一条路径：原来的整行裁决（右支），重演字段必须等于右支行
    decide(client, route, 0, item["id"], "right")
    whole = rehearse(client, route, 1)
    expect(whole["status"] == "passed", "整行裁决重演应通过")
    sealed = client.call(route + "/seal", {"revision": 1})
    db = open_candidate(client, route)
    try:
        row = db.execute("SELECT note,payload,weight FROM node WHERE bench='north' AND slot=1").fetchone()
        expect(row == ("祖本节点", b"\xff\x00", 2.25), f"整行裁决重演字段不符: {row}")
    finally:
        db.close()
    html = client.page(f"/plans/{plan['id']}")
    expect("已经采纳" not in html, "整行裁决不应显示列合成采纳标记")
    # 封存后不能再改裁决，另开计划演示列合成路径
    expect(client.call(route + "/decisions",
                       {"revision": 1, "item": item["id"], "choice": "clear"}, 409)["code"] == "closed_plan",
           "封存计划仍可编辑")

    plan2 = client.call("/api/plans", {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)
    route2, item2 = f"/api/plans/{plan2['id']}", conflict_item(plan2)
    # 网页表单方式提交列合成（无 JS 时 col.* 字段同样可用）
    result = decide(client, route2, 0, item2["id"], "combine",
                    {"note": "left", "payload": "right", "weight": "right"}, form=True)
    expect(result["choice"] == "combine" and result["revision"] == 1, "列合成裁决未保存或未推进版本")
    refreshed = client.call(route2)
    picked = next(i for i in refreshed["items"] if i["id"] == item2["id"])
    expect(picked["choice"] == "combine" and picked["synthesis"]["row"] ==
           [["t", "north"], ["i", "1"], ["t", "左支备注"], ["b", "/wA="], ["r", 2.25.hex()]],
           "API 返回的合成行与双方独立列改动不一致")
    html = client.page(f"/plans/{plan2['id']}")
    expect("已经采纳 · 列合成" in html, "页面未把列合成标记为已经采纳")
    expect("左支备注" in html and "/wA=" in html and 2.25.hex() in html, "页面未展示合成结果字段")
    attempt = rehearse(client, route2, 1)
    expect(attempt["status"] == "passed" and attempt["changed"] == 1, "列合成候选应通过重演")
    client.call(route2 + "/seal", {"revision": 1})
    db = open_candidate(client, route2)
    try:
        row = db.execute("SELECT note,payload,weight FROM node WHERE bench='north' AND slot=1").fetchone()
        expect(row == ("左支备注", b"\xff\x00", 2.25), f"列合成重演字段不符: {row}")
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], "合成结果存在外键违规")
    finally:
        db.close()
    print("combine ✓ 整行裁决与列合成各演示一次，页面信息与重演字段一致")


def scenario_conflict(client):
    base = client.upload("conf-base", make_node_variant(None))
    left = client.upload("conf-left", make_node_variant("conflict-left"))
    right = client.upload("conf-right", make_node_variant("conflict-right"))
    plan = client.call("/api/plans", {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)
    route = f"/api/plans/{plan['id']}"
    item = conflict_item(plan)
    expect(item["merge"]["combinable"] is False and item["merge"]["blockers"] == ["column_conflict"],
           "同列不同值应阻断一键合成")
    expect(item["merge"]["conflict_columns"] == ["note"], "冲突列识别不正确")
    html = client.page(f"/plans/{plan['id']}")
    expect("同列冲突" in html and "须逐列选定来源" in html, "页面未说明同列冲突处理路径")
    # 缺少冲突列的显式选择必须被拒绝
    refused = decide(client, route, 0, item["id"], "combine",
                     {"weight": "left", "payload": "right"}, status=409)
    expect(refused["code"] == "column_conflict", "未决冲突列未被拦截")
    # 逐列裁决：note 选右支，weight 选左支，payload 选右支
    decide(client, route, 0, item["id"], "combine",
           {"note": "right", "weight": "left", "payload": "right"})
    attempt = rehearse(client, route, 1)
    expect(attempt["status"] == "passed", "逐列裁决后的重演应通过")
    client.call(route + "/seal", {"revision": 1})
    db = open_candidate(client, route)
    try:
        row = db.execute("SELECT note,payload,weight FROM node WHERE bench='north' AND slot=1").fetchone()
        expect(row == ("右支备注", b"\xab", 4.0), f"逐列裁决重演字段不符: {row}")
    finally:
        db.close()
    print("conflict ✓ 同列不同值必须显式逐列选边，重演字段与选择一致")


def scenario_deletion(client):
    base = client.upload("del-base", make_node_variant(None))
    left = client.upload("del-left", make_node_variant("delete-left"))
    right = client.upload("del-right", make_node_variant("delete-right"))
    sources = {"base": base["id"], "left": left["id"], "right": right["id"]}
    plan = client.call("/api/plans", sources, 201)
    route = f"/api/plans/{plan['id']}"
    item = conflict_item(plan)
    expect(item["merge"]["blockers"] == ["row_missing"], "删除碰上修改应阻断列合成")
    html = client.page(f"/plans/{plan['id']}")
    expect("删除与修改相遇" in html and "不可合成" in html, "页面未说明删除处理路径")
    refused = decide(client, route, 0, item["id"], "combine", {"note": "right"}, status=409)
    expect(refused["code"] == "row_missing", "缺席行上的列合成未被拦截")
    # 处理路径一：采纳删除（左支缺席），封存后下载验证行确实消失
    decide(client, route, 0, item["id"], "left")
    expect(rehearse(client, route, 1)["status"] == "passed", "采纳删除的重演应通过")
    client.call(route + "/seal", {"revision": 1})
    db = open_candidate(client, route)
    try:
        gone = db.execute("SELECT 1 FROM node WHERE bench='north' AND slot=1").fetchone()
        expect(gone is None, "采纳删除后行仍存在")
        expect(db.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 1, "其余行被误删")
    finally:
        db.close()
    # 处理路径二：另开计划整行保留右支修改，验证字段保持
    plan2 = client.call("/api/plans", sources, 201)
    route2, item2 = f"/api/plans/{plan2['id']}", conflict_item(plan2)
    decide(client, route2, 0, item2["id"], "right")
    expect(rehearse(client, route2, 1)["status"] == "passed", "整行保留的重演应通过")
    client.call(route2 + "/seal", {"revision": 1})
    db = open_candidate(client, route2)
    try:
        row = db.execute("SELECT note FROM node WHERE bench='north' AND slot=1").fetchone()
        expect(row == ("右支备注",), f"整行保留的重演字段不符: {row}")
    finally:
        db.close()
    print("deletion ✓ 删除碰上修改只能整行裁决，删除与保留两条路径都可重演")


def scenario_constraint(client):
    base = client.upload("pair-base", make_pair_variant(None))
    left = client.upload("pair-left", make_pair_variant("left"))
    right = client.upload("pair-right", make_pair_variant("right"))
    plan = client.call("/api/plans", {"base": base["id"], "left": left["id"], "right": right["id"]}, 201)
    route = f"/api/plans/{plan['id']}"
    item = conflict_item(plan)
    expect(item["merge"]["combinable"] is True, "各自合法的独立列改动应允许合成")
    decide(client, route, 0, item["id"], "combine", {"weight": "left", "span": "right"})
    blocked = rehearse(client, route, 1)
    expect(blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint",
           "违反行级约束的合成应被重演拦截")
    expect(client.call(route + "/download", status=404)["code"] == "unpublished", "受阻候选仍可下载")
    html = client.page(f"/plans/{plan['id']}")
    expect("已经采纳 · 列合成" in html and "受阻" in html, "页面应同时显示已采纳合成与重演受阻")
    # 明确处理路径：退回整行裁决，选择右支后重演通过
    decide(client, route, 1, item["id"], "right")
    passed = rehearse(client, route, 2)
    expect(passed["status"] == "passed", "整行裁决应解除约束冲突")
    client.call(route + "/seal", {"revision": 2})
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(client.call(route + "/download"))
        row = db.execute("SELECT span,weight FROM pair WHERE id='k1'").fetchone()
        expect(row == (6, 4.0), f"约束冲突后的整行裁决字段不符: {row}")
    finally:
        db.close()
    print("constraint ✓ 独立列拼成违反行级约束时重演受阻，整行裁决路径仍然可用")


SCENARIOS = {"combine": scenario_combine, "conflict": scenario_conflict,
             "deletion": scenario_deletion, "constraint": scenario_constraint}


def main():
    parser = argparse.ArgumentParser(description="验证 Pagefold 列合成工作流")
    parser.add_argument("scenario", choices=("all", *SCENARIOS))
    with tempfile.TemporaryDirectory(prefix="pagefold-columns-") as root:
        from pathlib import Path
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                scenario = parser.parse_args().scenario
                names = tuple(SCENARIOS) if scenario == "all" else (scenario,)
                for name in names:
                    SCENARIOS[name](client)
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
