"""Pagefold stress 合流三现场的可复核诊断脚本。

本脚本只读取仓库内 pagefold 包的公共入口，通过真实 HTTP 接口登记镜像、
建立计划、重演、封存与下载，并在最后用重演引擎直接验证处理顺序无关性。
它不修改 pagefold 包的任何代码，运行结果写入一份 JSON 证据文件。

三个现场：
  A 原样例   demo.py 生成的 quartz + stress-left + stress-right，
             bench.label 为整表 UNIQUE，重演被唯一约束挡住。
  B 谓词对照 把整表 UNIQUE 换成部分唯一索引
             CREATE UNIQUE INDEX ... ON bench(label) WHERE shared=1。
             B1 两行都在谓词内，仍相撞；B2 把右支 shared 改为 0，
             其中一行落在谓词之外，持久化通过并可下载。
  C 外键对照 保持原样例模式，仅改输入：左支删除 node 父行并连带删除其
             binding 子行，右支为同一父行新增第二个 binding；合流自动
             采纳两侧后产生孤儿子行，首个错误变为真正的外键失败。

用法：
  python3 -I diagnostics/stress_triage.py            # 运行全部断言并打印结论
  python3 -I diagnostics/stress_triage.py --out FILE # 同时写 JSON 证据
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pagefold import replay
from pagefold.catalog import describe
from pagefold.demo import SCHEMA, image
from pagefold.errors import Problem
from pagefold.merge import calculate
from pagefold.rows import read_image
from pagefold.server import make_server
from pagefold.sqlite_image import mounted
from pagefold.store import Store

# 部分唯一索引模式（现场 B）：除 bench 外，node/binding/索引与原样例一致，
# 因此三份 B 镜像之间的 sqlite_schema 记录仍然精确匹配。
SCHEMA_PARTIAL = """
CREATE TABLE bench(
    code TEXT PRIMARY KEY, label TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE UNIQUE INDEX bench_shared_label ON bench(label) WHERE shared=1;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    note TEXT, payload BLOB, weight REAL CHECK(weight >= 0),
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE INDEX node_weight ON node(weight);
"""


def build(schema, seed, edit):
    """在独立内存库中构造一份完整、外键有效的检查点镜像。"""
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema)
        seed(db)
        edit(db)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def seed_demo(db):
    db.executemany("INSERT INTO bench VALUES(?,?)",
                   [("north", "玄石"), ("south", "砂层")])
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?)",
                   [("north", 1, "祖本节点", b"\x00\x14", 1.5),
                    ("south", 1, "稳定节点", b"", 3.0)])
    db.execute("INSERT INTO binding VALUES(1,'north',1)")


def seed_partial(db):
    db.executemany("INSERT INTO bench VALUES(?,?,?)",
                   [("north", "玄石", 0), ("south", "砂层", 0)])
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?)",
                   [("north", 1, "祖本节点", b"\x00\x14", 1.5),
                    ("south", 1, "稳定节点", b"", 3.0)])
    db.execute("INSERT INTO binding VALUES(1,'north',1)")


def noop(_db):
    pass


class Client:
    def __init__(self, origin):
        self.origin = origin

    def call(self, route, data=None, status=200, raw=None, headers=None):
        headers = dict(headers or {})
        if raw is not None:
            payload = data = raw
        elif isinstance(data, dict):
            payload = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            payload = data
        request = Request(self.origin + route, data=payload, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                body, code = response.read(), response.status
        except HTTPError as error:
            with error:
                body, code = error.read(), error.code
        assert code == status, f"{route}: {code} != {status}: {body[:300]!r}"
        return json.loads(body) if body[:1] in (b"{", b"[") else body

    def upload(self, label, blob):
        return self.call("/api/images", raw=blob, status=201, headers={
            "X-Image-Name": label, "Content-Type": "application/octet-stream"})["id"]

    def run_plan(self, label, triple):
        ids = {role: self.upload(f"{label}:{role}", blob)
               for role, blob in zip(("base", "left", "right"), triple)}
        plan = self.call("/api/plans", ids, 201)
        result = self.call(f"/api/plans/{plan['id']}/rehearse",
                           {"revision": plan["revision"]})
        return ids, plan, result


def source_health(blob):
    """登记前源库自身的完整性证据。"""
    with mounted(blob) as db:
        return {
            "integrity_check": db.execute("PRAGMA main.integrity_check").fetchone()[0],
            "foreign_key_check": db.execute("PRAGMA main.foreign_key_check").fetchall(),
            "schema": [list(row) for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM main.sqlite_schema "
                "WHERE substr(name,1,7)<>'sqlite_' ORDER BY type,name")],
        }


def reverse_order_check(triple):
    """同一组操作在正序、逆序表迭代下的首个错误必须一致。"""
    with mounted(triple[0]) as db:
        catalog = describe(db)
    snapshots = []
    for blob in triple:
        with mounted(blob) as db:
            snapshots.append(read_image(db, catalog))
    items = calculate(*snapshots)
    operations = replay.amendments(items, {})
    outcomes = {}
    original = list(catalog["tables"])
    for label, tables in (("catalog_order", original),
                          ("reversed_order", list(reversed(original)))):
        catalog["tables"] = tables
        try:
            replay.rebuild(triple[0], catalog, operations)
            outcomes[label] = {"status": "passed"}
        except Problem as exc:
            outcomes[label] = {"status": "blocked", "code": exc.code, "detail": exc.detail}
        finally:
            catalog["tables"] = original
    outcomes["same_first_error"] = (
        outcomes["catalog_order"] == outcomes["reversed_order"])
    return outcomes


def main():
    parser = argparse.ArgumentParser(description="复现 stress 合流三个现场并固化证据")
    parser.add_argument("--out", type=Path, help="JSON 证据输出路径")
    arguments = parser.parse_args()

    # --- 现场 A：仓库原样例（整表 UNIQUE） ---
    scene_a = (image("quartz"), image("stress-left"), image("stress-right"))

    # --- 现场 B：部分唯一索引，B1 双行在谓词内，B2 右行行落到谓词外 ---
    b_base = build(SCHEMA_PARTIAL, seed_partial, noop)
    b1_left = build(SCHEMA_PARTIAL, seed_partial,
                    lambda db: db.execute("INSERT INTO bench VALUES(?,?,?)",
                                          ("stress-left", "共享键值", 1)))
    b1_right = build(SCHEMA_PARTIAL, seed_partial,
                     lambda db: db.execute("INSERT INTO bench VALUES(?,?,?)",
                                           ("stress-right", "共享键值", 1)))
    b2_right = build(SCHEMA_PARTIAL, seed_partial,
                     lambda db: db.execute("INSERT INTO bench VALUES(?,?,?)",
                                           ("stress-right", "共享键值", 0)))

    # --- 现场 C：同模式改输入，跨支拆分制造孤儿子行（真正的外键失败） ---
    c_base = build(SCHEMA, seed_demo, noop)
    c_left = build(SCHEMA, seed_demo, lambda db: (
        db.execute("DELETE FROM binding WHERE id=1"),
        db.execute("DELETE FROM node WHERE bench='north' AND slot=1")))
    c_right = build(SCHEMA, seed_demo,
                    lambda db: db.execute("INSERT INTO binding VALUES(2,'north',1)"))

    with tempfile.TemporaryDirectory(prefix="pagefold-triage-") as root:
        with make_server(Store(Path(root) / "state.sqlite"), 0) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = Client(f"http://127.0.0.1:{server.server_port}")
            try:
                evidence = collect(client, scene_a, (b_base, b1_left, b1_right),
                                   (b_base, b1_left, b2_right), (c_base, c_left, c_right))
            finally:
                server.shutdown()
                thread.join()

    # 引擎级顺序无关性（不经 HTTP，直接调用重演模块）
    evidence["order_independence"] = {
        "scene_A": reverse_order_check(scene_a),
        "scene_C": reverse_order_check((c_base, c_left, c_right)),
    }
    evidence["source_health"] = {
        "scene_A": [source_health(blob) for blob in scene_a],
        "scene_B1": [source_health(blob) for blob in (b_base, b1_left, b1_right)],
        "scene_B2": [source_health(blob) for blob in (b_base, b1_left, b2_right)],
        "scene_C": [source_health(blob) for blob in (c_base, c_left, c_right)],
    }

    assert_expectations(evidence)
    if arguments.out:
        arguments.out.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
        print(f"证据已写入 {arguments.out}")
    print_summary(evidence)


def collect(client, scene_a, scene_b1, scene_b2, scene_c):
    evidence = {"scenes": {}}
    for name, triple in (("A_full_unique", scene_a),
                         ("B1_partial_inside", scene_b1),
                         ("B2_partial_outside", scene_b2),
                         ("C_foreign_key", scene_c)):
        ids, plan, result = client.run_plan(name, triple)
        entry = {
            "source_image_ids": ids,
            "revision": plan["revision"],
            "state": plan["state"],
            "unresolved": plan["unresolved"],
            "items": [{k: item[k] for k in ("id", "table", "key", "automatic", "choice")}
                      for item in plan["items"]],
            "rehearsal": result,
        }
        if result["status"] == "blocked":
            plan_id = result["id"]
            entry["download_gate"] = client.call(
                f"/api/plans/{plan_id}/download", status=404)
            entry["seal_gate"] = client.call(
                f"/api/plans/{plan_id}/seal", {"revision": result["revision"]}, status=409)
            # 同版本再次重演必须复用同一条 attempts 证据
            entry["repeat_rehearsal"] = client.call(
                f"/api/plans/{plan_id}/rehearse", {"revision": result["revision"]})
            entry["attempt_is_reused"] = entry["repeat_rehearsal"] == result
        else:
            plan_id = result["id"]
            stale = client.call(f"/api/plans/{plan_id}/decisions",
                                {"revision": 99, "item": "0" * 64, "choice": "base"},
                                status=409)
            certificate = client.call(f"/api/plans/{plan_id}/seal",
                                      {"revision": result["revision"]})
            candidate = client.call(f"/api/plans/{plan_id}/download")
            with sqlite3.connect(":memory:") as db:
                db.deserialize(candidate)
                entry["persisted_bench_rows"] = db.execute(
                    "SELECT * FROM bench ORDER BY code").fetchall()
                entry["persisted_foreign_key_check"] = db.execute(
                    "PRAGMA foreign_key_check").fetchall()
            entry["stale_revision_probe"] = stale
            entry["candidate_sha256_matches_certificate"] = (
                hashlib.sha256(candidate).hexdigest()
                == certificate["candidate_digest"])
        evidence["scenes"][name] = entry
    return evidence


def assert_expectations(evidence):
    scenes = evidence["scenes"]
    for name in ("A_full_unique", "B1_partial_inside", "B2_partial_outside", "C_foreign_key"):
        scene = scenes[name]
        assert scene["unresolved"] == 0, f"{name} 出现待决项"
        assert scene["revision"] == 0 and scene["state"] == "open", f"{name} 版本/状态异常"
        assert all(item["automatic"] for item in scene["items"]), f"{name} 存在非自动项"

    a = scenes["A_full_unique"]["rehearsal"]
    assert a["status"] == "blocked" and a["problem"]["code"] == "constraint"
    assert "UNIQUE constraint failed: bench.label" in a["problem"]["detail"]
    assert scenes["A_full_unique"]["download_gate"]["code"] == "unpublished"
    assert scenes["A_full_unique"]["seal_gate"]["code"] == "rehearsal_required"
    assert scenes["A_full_unique"]["attempt_is_reused"]
    a_ids = {item["key"][0][1] for item in scenes["A_full_unique"]["items"]}
    assert a_ids == {"stress-left", "stress-right"}

    b1 = scenes["B1_partial_inside"]["rehearsal"]
    assert b1["status"] == "blocked" and b1["problem"]["code"] == "constraint"
    assert "UNIQUE constraint failed: bench.label" in b1["problem"]["detail"]

    b2 = scenes["B2_partial_outside"]["rehearsal"]
    assert b2["status"] == "passed" and b2["candidate_digest"]
    assert scenes["B2_partial_outside"]["stale_revision_probe"]["code"] == "stale_revision"
    assert scenes["B2_partial_outside"]["candidate_sha256_matches_certificate"]
    assert scenes["B2_partial_outside"]["persisted_foreign_key_check"] == []
    bench_rows = scenes["B2_partial_outside"]["persisted_bench_rows"]
    assert ("stress-left", "共享键值", 1) in bench_rows
    assert ("stress-right", "共享键值", 0) in bench_rows

    c = scenes["C_foreign_key"]["rehearsal"]
    assert c["status"] == "blocked" and c["problem"]["code"] == "foreign_key"
    assert "binding" in c["problem"]["detail"] and "node" in c["problem"]["detail"]
    assert scenes["C_foreign_key"]["download_gate"]["code"] == "unpublished"

    for group in evidence["source_health"].values():
        for health in group:
            assert health["integrity_check"] == "ok"
            assert health["foreign_key_check"] == []

    for name in ("scene_A", "scene_C"):
        order = evidence["order_independence"][name]
        assert order["same_first_error"], name
        assert order["catalog_order"]["status"] == "blocked"


def print_summary(evidence):
    print("\n=== 现场结论 ===")
    labels = {
        "A_full_unique": "A 原样例：整表 UNIQUE 相撞",
        "B1_partial_inside": "B1 部分索引谓词内：仍然相撞",
        "B2_partial_outside": "B2 一行落到谓词外：持久化通过",
        "C_foreign_key": "C 跨支拆分：首个错误是真正的外键失败",
    }
    for name, scene in evidence["scenes"].items():
        result = scene["rehearsal"]
        detail = result.get("problem", {}).get("detail",
                                                f"digest={result.get('candidate_digest', '')[:12]}")
        print(f"[{labels[name]}]\n  unresolved={scene['unresolved']} "
              f"status={result['status']}\n  {detail}")
    print("\n源库健康：全部 integrity_check=ok、foreign_key_check=[]")
    for name, order in evidence["order_independence"].items():
        print(f"顺序无关 {name}: {order['same_first_error']} "
              f"({order['catalog_order']['code']})")


if __name__ == "__main__":
    main()
