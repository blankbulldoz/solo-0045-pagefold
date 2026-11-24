import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread

from examples.walkthrough import Client, expect
from pagefold.codec import fingerprint
from pagefold.demo import image
from pagefold.server import make_server
from pagefold.store import Store

WORKFLOWS = ("prepare", "drift", "deliver", "reclaim")

LEDGER_SCHEMA = """
CREATE TABLE ledger(code TEXT PRIMARY KEY, title TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE entry(
    ledger TEXT NOT NULL REFERENCES ledger(code), seq INTEGER NOT NULL,
    memo TEXT, amount REAL CHECK(amount >= 0),
    PRIMARY KEY(ledger,seq)
) WITHOUT ROWID;
"""


def ledger_image(flavor):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(LEDGER_SCHEMA)
        db.executemany("INSERT INTO ledger VALUES(?,?)", [("ops", "运维台账"), ("fin", "财务台账")])
        db.executemany("INSERT INTO entry VALUES(?,?,?,?)",
                       [("ops", 1, "祖本记录", 10.0), ("fin", 1, "稳定记录", 20.0)])
        if flavor == "left":
            db.execute("UPDATE entry SET memo='左支修订' WHERE ledger='ops' AND seq=1")
            db.execute("INSERT INTO entry VALUES('fin',2,'左支补充',5.0)")
        elif flavor == "right":
            db.execute("UPDATE entry SET amount=12.5 WHERE ledger='ops' AND seq=1")
            db.execute("UPDATE ledger SET title='右支更名' WHERE code='fin'")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def open_sqlite(data):
    db = sqlite3.connect(":memory:")
    db.deserialize(data)
    return db


def conflict_of(plan):
    return next(item for item in plan["items"] if not item["automatic"])


def run(client, goal):
    def done(workflow, result):
        if goal in (workflow, "all"):
            print(f"{workflow} ✓ {result}")
        return goal == workflow

    quartz, cerulean, umber = (client.upload(name)["id"] for name in ("quartz", "cerulean", "umber"))
    plan_a = client.call("/api/plans", {"base": quartz, "left": cerulean, "right": umber}, 201)
    route_a = f"/api/plans/{plan_a['id']}"
    conflict_a = conflict_of(plan_a)["id"]
    client.call(route_a + "/decisions", {"item": conflict_a, "choice": "right", "revision": 0})
    client.call(route_a + "/decisions", {"item": conflict_a, "choice": "base", "revision": 1})
    passed_a = client.call(route_a + "/rehearse", {"revision": 2})
    expect(passed_a["status"] == "passed", "计划 A 第 2 版重演未通过")
    ledger_base = client.upload("ledger-base", ledger_image("base"))["id"]
    ledger_left = client.upload("ledger-left", ledger_image("left"))["id"]
    ledger_right = client.upload("ledger-right", ledger_image("right"))["id"]
    plan_b = client.call("/api/plans", {"base": ledger_base, "left": ledger_left, "right": ledger_right}, 201)
    route_b = f"/api/plans/{plan_b['id']}"
    conflict_b = conflict_of(plan_b)["id"]
    client.call(route_b + "/decisions", {"item": conflict_b, "choice": "left", "revision": 0})
    passed_b1 = client.call(route_b + "/rehearse", {"revision": 1})
    expect(passed_b1["status"] == "passed", "计划 B 第 1 版重演未通过")
    expect(client.call("/api/bundles") == [], "交付列表应为空")
    if done("prepare", "两份计划各自独立预检：A 推进到第 2 版、B 第 1 版均有合格候选"):
        return
    client.call(route_b + "/decisions", {"item": conflict_b, "choice": "right", "revision": 1})
    drift = client.call("/api/bundles", {"members": [{"plan": plan_a["id"], "revision": 2},
                                                     {"plan": plan_b["id"], "revision": 1}]}, 409)
    expect(drift["code"] == "revision_drift", "版本漂移未整组拒绝")
    expect("第 2 版" in drift["detail"] and "第 1 版" in drift["detail"], "漂移诊断未指明版本")
    missing = client.call("/api/bundles", {"members": [{"plan": plan_a["id"], "revision": 2},
                                                       {"plan": plan_b["id"], "revision": 2}]}, 409)
    expect(missing["code"] == "candidate_missing", "缺合格候选未整组拒绝")
    expect(client.call("/api/bundles") == [], "整组失败后不应留下交付")
    if done("drift", "准备期间改选使 B 漂移：整组失败且无任何交付落库，历史候选仍在但不可领取"):
        return
    passed_b2 = client.call(route_b + "/rehearse", {"revision": 2})
    expect(passed_b2["status"] == "passed", "计划 B 第 2 版重演未通过")
    cert_a = client.call(route_a + "/seal", {"revision": 2})
    cert_b = client.call(route_b + "/seal", {"revision": 2})
    selection = [{"plan": plan_b["id"], "revision": 2}, {"plan": plan_a["id"], "revision": 2}]
    manifest = client.call("/api/bundles", {"members": selection}, 201)
    expect(manifest["format"] == "pagefold-bundle-v1", "清单格式不符")
    expect(manifest["id"] == fingerprint(manifest["members"]), "交付身份不等于成员清单摘要")
    expect([m["plan"] for m in manifest["members"]] == sorted([plan_a["id"], plan_b["id"]]),
           "成员未按计划身份规范排序")
    by_plan = {m["plan"]: m for m in manifest["members"]}
    for plan_id, cert in ((plan_a["id"], cert_a), (plan_b["id"], cert_b)):
        member = by_plan[plan_id]
        expect(dict(member, format="pagefold-certificate-v1") == cert, "成员记录与原有封存凭证不一致")
    expect(len({m["candidate_digest"] for m in manifest["members"]}) == 2, "两份候选不应相同")
    expect(client.call("/api/bundles") == [{"id": manifest["id"], "members": 2}], "交付列表不符")
    page = client.call(f"/bundles/{manifest['id']}")
    expect(manifest["id"].encode() in page and "组合成员".encode() in page, "交付页未渲染成员关系")
    print(f"  交付清单 {manifest['id']}")
    for member in manifest["members"]:
        print(f"  ├─ 计划 {member['plan'][:12]}… 第 {member['revision']} 版 · "
              f"候选 {member['candidate_digest'][:16]}… · 与原有封存凭证逐项一致")
    print("  └─ 两份计划同为第 2 版但身份各自独立；清单摘要绑定全部成员，构成同一套")
    if done("deliver", "成功发布：成员记录与原有凭证一致，同一版号在两份计划身份不同"):
        return
    expect(client.call(f"/api/bundles/{manifest['id']}") == manifest, "重新领取的清单发生变化")
    again = client.call("/api/bundles", {"members": selection[::-1]}, 200)
    expect(again == manifest, "同一套选择重复提交未返回同一交付")
    for member in manifest["members"]:
        body = client.call(f"/api/bundles/{manifest['id']}/members/{member['plan']}")
        expect(hashlib.sha256(body).hexdigest() == member["candidate_digest"], "成员内容与清单摘要不符")
        expect(body == client.call(f"/api/plans/{member['plan']}/download"), "成员与计划封存成果不一致")
    db_a = open_sqlite(client.call(f"/api/bundles/{manifest['id']}/members/{plan_a['id']}"))
    db_b = open_sqlite(client.call(f"/api/bundles/{manifest['id']}/members/{plan_b['id']}"))
    try:
        expect(db_a.execute("PRAGMA foreign_key_check").fetchall() == [], "A 外键违规")
        expect(db_a.execute("SELECT note FROM node WHERE bench='north' AND slot=1").fetchone() == ("祖本节点",),
               "A 中裁决结果不符")
        expect(db_b.execute("PRAGMA foreign_key_check").fetchall() == [], "B 外键违规")
        expect(db_b.execute("SELECT memo,amount FROM entry WHERE ledger='ops' AND seq=1").fetchone()
               == ("祖本记录", 12.5), "B 中改选后的裁决结果不符")
    finally:
        db_a.close()
        db_b.close()
    expect(client.call(f"/api/bundles/{manifest['id']}/members/{'0' * 32}", status=404)["code"]
           == "unknown_member", "非成员计划未拒绝")
    expect(client.call(f"/api/bundles/{'0' * 64}", status=404)["code"] == "unknown_bundle", "未知交付未拒绝")
    expect(client.call("/api/bundles", {"members": [{"plan": plan_a["id"], "revision": 2}]}, 400)["code"]
           == "members", "单成员未拒绝")
    expect(client.call("/api/bundles", {"members": [{"plan": plan_a["id"], "revision": 2},
                                                    {"plan": plan_a["id"], "revision": 2}]}, 409)["code"]
           == "members", "重复成员未拒绝")
    expect(client.call("/api/bundles", {"members": [{"plan": plan_a["id"], "revision": 2},
                                                    {"plan": "0" * 32, "revision": 0}]}, 404)["code"]
           == "unknown_plan", "未知计划未拒绝")
    plan_c = client.call("/api/plans", {"base": quartz, "left": cerulean, "right": umber}, 201)
    expect(client.call("/api/bundles", {"members": [{"plan": plan_a["id"], "revision": 2},
                                                    {"plan": plan_c["id"], "revision": 0}]}, 409)["code"]
           == "candidate_missing", "未重演计划未整组拒绝")
    done("reclaim", "重复领取逐字节一致：清单、成员摘要、封存成果与 SQLite 内容全部可核对")


def main():
    parser = argparse.ArgumentParser(description="演练 Pagefold 多计划联合交付")
    parser.add_argument("workflow", choices=("all", *WORKFLOWS))
    with tempfile.TemporaryDirectory(prefix="pagefold-joint-") as root:
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
