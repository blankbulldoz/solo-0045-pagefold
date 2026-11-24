"""结构桥接演练：不同列次序与列重命名两个例子。

两个例子共用同一祖本，分支各自演化出名称或次序不同但语义对应的列。
每个例子依次运行：试算（桥接与三方差异的预演，不落库）、原身份校验
（每个原行恰好映射到一个共同键空间的行）、合流后约束验证（重演候选的
完整性、外键与检查约束）。最后展示到不了同一目标的结构如何被识别。
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pagefold.server import make_server
from pagefold.store import Store

BASE_DDL = """
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

LEFT_REORDERED_DDL = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    slot INTEGER NOT NULL, bench TEXT NOT NULL REFERENCES bench(code),
    weight REAL CHECK(weight >= 0), note TEXT, payload BLOB,
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""

RIGHT_REORDERED_DDL = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    payload BLOB, weight REAL CHECK(weight >= 0), note TEXT,
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""

LEFT_RENAMED_DDL = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    memo TEXT, payload BLOB, weight REAL CHECK(weight >= 0),
    severity TEXT NOT NULL DEFAULT 'low',
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""

RIGHT_RENAMED_DDL = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    note TEXT, payload BLOB, weight REAL CHECK(weight >= 0),
    severity TEXT NOT NULL,
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""

RIGHT_PLUS_DDL = RIGHT_RENAMED_DDL.replace(
    "severity TEXT NOT NULL,", "severity TEXT NOT NULL, checksum BLOB NOT NULL,")

LEFT_STRICT_DDL = LEFT_RENAMED_DDL.replace(
    "severity TEXT NOT NULL DEFAULT 'low'", "severity TEXT NOT NULL")

NODE_ROWS = [("north", 1, "祖本节点", b"\x00\x14", 1.5),
             ("south", 1, "稳定节点", b"", 3.0),
             ("north", 2, "静默节点", b"\x01", 0.5)]

REORDER_BRIDGE = {"target": "base", "tables": [
    {"name": "bench", "key": ["code"]},
    {"name": "node", "key": ["bench", "slot"]},
    {"name": "binding", "key": ["id"]}]}

RENAME_BRIDGE = {"target": "left", "tables": [
    {"name": "bench", "key": ["code"]},
    {"name": "binding", "key": ["id"]},
    {"name": "node", "key": ["bench", "slot"], "columns": [
        {"name": "bench"}, {"name": "slot"},
        {"name": "memo", "from": {"base": "note", "right": "note"}},
        {"name": "payload"}, {"name": "weight"},
        {"name": "severity", "from": {"base": None}}]}]}


def expect(condition, description):
    if not condition:
        raise RuntimeError(description)


def build(ddl, statements):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(ddl)
        for sql, params in statements:
            db.execute(sql, params)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def base_image():
    return build(BASE_DDL, [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        *[("INSERT INTO node VALUES(?,?,?,?,?)", row) for row in NODE_ROWS],
        ("INSERT INTO binding VALUES(1,'north',1)", ())])


def reorder_left():
    rows = [("INSERT INTO node(slot,bench,weight,note,payload) VALUES(?,?,?,?,?)",
             (slot, bench, weight, note, payload)) for bench, slot, note, payload, weight in NODE_ROWS]
    return build(LEFT_REORDERED_DDL, [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        *rows,
        ("INSERT INTO binding VALUES(1,'north',1)", ()),
        ("UPDATE node SET note='左支覆写' WHERE bench='north' AND slot=1", ()),
        ("INSERT INTO node(slot,bench,weight,note,payload) VALUES(2,'south',4.0,'左支新增',NULL)", ())])


def reorder_right():
    rows = [("INSERT INTO node(payload,weight,note,bench,slot) VALUES(?,?,?,?,?)",
             (payload, weight, note, bench, slot)) for bench, slot, note, payload, weight in NODE_ROWS]
    return build(RIGHT_REORDERED_DDL, [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        *rows,
        ("INSERT INTO binding VALUES(1,'north',1)", ()),
        ("UPDATE node SET weight=2.25 WHERE bench='north' AND slot=1", ()),
        ("UPDATE bench SET label='右支更名' WHERE code='south'", ())])


def rename_left(strict=False):
    ddl = LEFT_STRICT_DDL if strict else LEFT_RENAMED_DDL
    rows = [("INSERT INTO node(bench,slot,memo,payload,weight,severity) VALUES(?,?,?,?,?,?)",
             (bench, slot, note, payload, weight, "low")) for bench, slot, note, payload, weight in NODE_ROWS]
    return build(ddl, [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        *rows,
        ("INSERT INTO binding VALUES(1,'north',1)", ()),
        ("UPDATE node SET memo='左支覆写',severity='high' WHERE bench='north' AND slot=1", ()),
        ("INSERT INTO node VALUES('south',2,'左支新增',NULL,4.0,'mid')", ()),
        ("INSERT INTO binding VALUES(2,'south',2)", ())])


def rename_right(plus=False):
    ddl = RIGHT_PLUS_DDL if plus else RIGHT_RENAMED_DDL
    severity = {("north", 1): "mid", ("south", 1): "mid", ("north", 2): "low"}
    rows = []
    for bench, slot, note, payload, weight in NODE_ROWS:
        if plus:
            rows.append(("INSERT INTO node VALUES(?,?,?,?,?,?,x'c0')",
                         (bench, slot, note, payload, weight, severity[(bench, slot)])))
        else:
            rows.append(("INSERT INTO node VALUES(?,?,?,?,?,?)",
                         (bench, slot, note, payload, weight, severity[(bench, slot)])))
    return build(ddl, [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        *rows,
        ("INSERT INTO binding VALUES(1,'north',1)", ()),
        ("UPDATE node SET note='右支修订' WHERE bench='north' AND slot=1", ()),
        ("UPDATE node SET weight=3.5 WHERE bench='south' AND slot=1", ())])


class Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, route, data=None, status=200):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(self.origin + route, data=body,
                          headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with urlopen(request, timeout=10) as result:
                raw, actual, kind = result.read(), result.status, result.headers.get_content_type()
        except HTTPError as error:
            with error:
                raw, actual, kind = error.read(), error.code, error.headers.get_content_type()
        expect(actual == status, f"{route}: {actual} != {status}: {raw[:300]}")
        return json.loads(raw) if kind == "application/json" else raw

    def upload(self, label, data):
        request = Request(self.origin + "/api/images", data=data,
                          headers={"Content-Type": "application/octet-stream",
                                   "X-Image-Name": quote(label)})
        with urlopen(request, timeout=10) as result:
            return json.loads(result.read())["id"]


def show_impact(report):
    for entry in report["tables"]:
        parts = [f"{entry['rows']} 行"]
        if entry["renamed"]:
            parts.append("改名 " + "、".join(f"{k}→{v}" for k, v in entry["renamed"].items()))
        if entry["filled"]:
            parts.append("填充 " + "、".join(f"{k}={v}" for k, v in entry["filled"].items()))
        if entry["reordered"]:
            parts.append("列次序已按名对齐")
        print(f"    {entry['table']}（{entry['role']}←{entry['source']}）：{'；'.join(parts)}")


def check_identity(report, expected):
    identity = report["identity"]
    for role, tables in expected.items():
        for table, rows in tables.items():
            counts = identity[role][table]
            expect(counts["rows_in"] == rows and counts["rows_out"] == rows,
                   f"{role}.{table} 原身份行数不符：{counts}")
            expect(counts["keys_remapped"] == 0, f"{role}.{table} 的键在桥接后改变")
    expect(identity["ancestor_rows_preserved"], "迁移后祖本行与桥接结果不一致")
    print("    原身份校验：各角色行数与键空间一一对应，迁移后祖本行与桥接结果一致")


def check_candidate(client, plan_id, expectations):
    certificate = client.call(f"/api/plans/{plan_id}/seal", {"revision": expectations["revision"]})
    expect(certificate["bridge"]["digest"] and certificate["schema"], "凭证缺少桥接绑定")
    candidate = client.call(f"/api/plans/{plan_id}/download")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        expect(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "候选完整性检查失败")
        expect(db.execute("PRAGMA foreign_key_check").fetchall() == [], "候选存在外键违规")
        columns = [row[1] for row in db.execute("PRAGMA table_info(node)")]
        expect(columns == expectations["columns"], f"候选列结构不符：{columns}")
        for sql, wanted in expectations["rows"]:
            got = db.execute(sql).fetchone()
            expect(got == wanted, f"候选行不符：{sql} → {got} != {wanted}")
    finally:
        db.close()
    print(f"    合流后约束验证：完整性、外键、检查约束全部通过，候选 {len(candidate)} 字节")


def example_reorder(client, base):
    print("【例一 · 不同列次序】分支以不同列次序重建 node 表，共同模式取祖本")
    left = client.upload("次序-左支", reorder_left())
    right = client.upload("次序-右支", reorder_right())
    sources = {"base": base, "left": left, "right": right}

    print("  试算（不落库）：")
    trial = client.call("/api/plans", {**sources, "bridge": REORDER_BRIDGE, "trial": True})
    expect(trial["trial"] and trial["reachable"], "列次序桥接试算不可达")
    expect(trial["bridge"]["alterations"] == [], "目标为祖本时不应需要物理迁移")
    show_impact(trial["bridge"])
    expect(trial["unresolved"] == 1 and len(trial["items"]) == 3, "试算差异项不符")

    check_identity(trial["bridge"], {"base": {"node": 3}, "left": {"node": 4}, "right": {"node": 3}})

    plan = client.call("/api/plans", {**sources, "bridge": REORDER_BRIDGE}, 201)
    expect(plan["bridge"]["target"] == "base", "计划未记录桥接目标")
    route = f"/api/plans/{plan['id']}"
    conflict = next(item for item in plan["items"] if not item["automatic"])
    client.call(route + "/decisions", {"item": conflict["id"], "choice": "right", "revision": 0})
    accepted = client.call(route + "/rehearse", {"revision": 1})
    expect(accepted["status"] == "passed" and accepted["changed"] == 3, "例一重演未通过")

    check_candidate(client, plan["id"], {"revision": 1,
        "columns": ["bench", "slot", "note", "payload", "weight"],
        "rows": [("SELECT note,weight FROM node WHERE bench='north' AND slot=1", ("祖本节点", 2.25)),
                 ("SELECT note FROM node WHERE bench='south' AND slot=2", ("左支新增",)),
                 ("SELECT label FROM bench WHERE code='south'", ("右支更名",))]})


def example_rename(client, base):
    print("【例二 · 列重命名】左支把 note 改名 memo 并加带默认值的 severity，"
          "右支已有同名 severity 旧值，含复合外键")
    left = client.upload("改名-左支", rename_left())
    right = client.upload("改名-右支", rename_right())
    sources = {"base": base, "left": left, "right": right}

    print("  试算（不落库）：")
    trial = client.call("/api/plans", {**sources, "bridge": RENAME_BRIDGE, "trial": True})
    expect(trial["trial"] and trial["reachable"], "重命名桥接试算不可达")
    alterations = trial["bridge"]["alterations"]
    expect(any("RENAME COLUMN" in sql for sql in alterations) and
           any("ADD COLUMN" in sql and "DEFAULT 'low'" in sql for sql in alterations),
           f"祖本迁移语句不符：{alterations}")
    print(f"    祖本迁移：{alterations}")
    show_impact(trial["bridge"])
    expect(trial["unresolved"] == 1 and len(trial["items"]) == 4, "试算差异项不符")

    check_identity(trial["bridge"], {"base": {"node": 3}, "left": {"node": 4}, "right": {"node": 3}})

    plan = client.call("/api/plans", {**sources, "bridge": RENAME_BRIDGE}, 201)
    route = f"/api/plans/{plan['id']}"
    fetched = client.call(route)
    expect(fetched["bridge"]["report"]["identity"]["ancestor_rows_preserved"], "计划未保留身份校验结果")
    page = client.call(f"/plans/{plan['id']}")
    expect("结构桥接".encode() in page and "note→memo".encode() in page, "计划页未展示桥接结果")
    conflict = next(item for item in plan["items"] if not item["automatic"])
    expect(conflict["base"][5] == ["t", "low"], "祖本行的 severity 应来自默认值填充")
    expect(conflict["right"][5] == ["t", "mid"], "右支行的 severity 应保留同名旧值")
    client.call(route + "/decisions", {"item": conflict["id"], "choice": "right", "revision": 0})
    accepted = client.call(route + "/rehearse", {"revision": 1})
    expect(accepted["status"] == "passed" and accepted["changed"] == 4, "例二重演未通过")

    check_candidate(client, plan["id"], {"revision": 1,
        "columns": ["bench", "slot", "memo", "payload", "weight", "severity"],
        "rows": [("SELECT memo,severity FROM node WHERE bench='north' AND slot=1", ("右支修订", "mid")),
                 ("SELECT memo,severity FROM node WHERE bench='north' AND slot=2", ("静默节点", "low")),
                 ("SELECT memo,severity FROM node WHERE bench='south' AND slot=2", ("左支新增", "mid")),
                 ("SELECT COUNT(*) FROM binding", (2,))]})
    return left, right


def example_unreachable(client, base, left, right):
    print("【到不了同一目标的部分】")
    right_plus = client.upload("改名-右支-多余列", rename_right(plus=True))
    left_strict = client.upload("改名-左支-无默认值", rename_left(strict=True))

    trial = client.call("/api/plans", {"base": base, "left": left,
                                       "right": right_plus, "bridge": RENAME_BRIDGE, "trial": True})
    unreachable = trial["bridge"]["unreachable"]
    expect(not trial["reachable"] and any(item["column"] == "checksum" and item["role"] == "right"
                                          for item in unreachable), "未识别右支多余列")
    print(f"    右支独有 checksum：{unreachable[0]['reason']}（{unreachable[0]['table']}."
          f"{unreachable[0]['column']}，角色 {unreachable[0]['role']}）")
    error = client.call("/api/plans", {"base": base, "left": left,
                                       "right": right_plus, "bridge": RENAME_BRIDGE}, 409)
    expect(error["code"] == "bridge_unreachable", "多余列未挡住计划创建")

    trial = client.call("/api/plans", {"base": base, "left": left_strict,
                                       "right": right, "bridge": RENAME_BRIDGE, "trial": True})
    unreachable = trial["bridge"]["unreachable"]
    expect(any(item["column"] == "severity" and item["role"] == "base" for item in unreachable),
           "未识别无法填充的 NOT NULL 列")
    print(f"    祖本缺席 severity 且目标无默认值：{unreachable[0]['reason']}")
    error = client.call("/api/plans", {"base": base, "left": left_strict,
                                       "right": right, "bridge": RENAME_BRIDGE}, 409)
    expect(error["code"] == "bridge_unreachable", "无法填充的列未挡住计划创建")


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-bridge-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                client = Client(server.server_port)
                base = client.upload("共同祖本", base_image())
                example_reorder(client, base)
                left, right = example_rename(client, base)
                example_unreachable(client, base, left, right)
            finally:
                server.shutdown()
                thread.join()
    print("全部桥接演练通过。")


if __name__ == "__main__":
    main()
