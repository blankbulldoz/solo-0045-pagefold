"""阻断集合演示：一次重演收全相互独立的约束问题，并交代 CHECK 掩盖外键的缺口。

运行：

    python3 -m examples.blockers_demo

脚本分两部分：

1. 直接驱动真实的 ``pagefold.replay.rebuild``：
   - 两个相互独立的约束组（CHECK 与 UNIQUE），任意输入次序得到同一个阻断集合；
   - 一组 CHECK 掩盖同条目外键的情况，分别给出 verified / unchecked / gaps；
   - 演示只修 CHECK 后外键如何浮出，以及完全恢复后的通过结果对比。
2. 走真实 HTTP 入口（临时状态库 + 本机服务）：
   - 一个计划里同时制造唯一键和外键两个独立组；
   - 阻断集合进入 ``POST /api/plans/{id}/rehearse`` 的原有结果与 attempts 历史；
   - 同版重演复用同一证据；改判恢复后重演通过，并可封存、下载、独立校验。

所有样本都在内存或临时目录里生成，退出码非 0 表示演示中的断言失败。
"""

import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from threading import Thread

if __package__ in (None, ""):  # 允许 python3 examples/blockers_demo.py 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from examples.walkthrough import Client
else:
    from .walkthrough import Client

from pagefold.catalog import describe
from pagefold.codec import canonical
from pagefold.errors import Problem
from pagefold.replay import rebuild
from pagefold.server import make_server
from pagefold.sqlite_image import mounted
from pagefold.store import Store

# -- 第一部分：rebuild 级别的阻断集合 ----------------------------------------

CORE_SCHEMA = """
CREATE TABLE team(code TEXT PRIMARY KEY, title TEXT NOT NULL UNIQUE);
CREATE TABLE member(
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    age INTEGER CHECK(age >= 0),
    team_code TEXT REFERENCES team(code)
);
CREATE TABLE assignment(
    id INTEGER PRIMARY KEY,
    scope TEXT CHECK(length(scope) >= 2) REFERENCES team(code)
);
"""

# 额外验证 WITHOUT ROWID 复合外键的行身份反查。
WR_SCHEMA = """
CREATE TABLE parent(a TEXT, b INTEGER, PRIMARY KEY(a,b)) WITHOUT ROWID;
CREATE TABLE child(ca TEXT, cb INTEGER, note TEXT,
    PRIMARY KEY(ca,cb),
    FOREIGN KEY(ca,cb) REFERENCES parent(a,b)) WITHOUT ROWID;
"""

failures = []


def check(condition, description):
    mark = "✓" if condition else "✗"
    print(f"  {mark} {description}")
    if not condition:
        failures.append(description)


def encoded_row(values):
    from pagefold.codec import atom
    return [atom(value) for value in values]


def encoded_key(values):
    from pagefold.rows import key_atom
    return [key_atom(value) for value in values]


def make_ancestor(schema, seed):
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(schema)
    for table, rows in seed.items():
        if not rows:
            continue
        marks = ",".join("?" for _ in rows[0])
        db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    db.commit()
    data = db.serialize()
    db.close()
    with mounted(data) as view:
        catalog = describe(view)
    return data, catalog


def change(table, key_values, row_values):
    key = encoded_key(key_values)
    return {"id": f"{table}:{canonical(key)}", "table": table, "key": key, "base": None}, \
        encoded_row(row_values)


def blocked(data, catalog, operations):
    try:
        rebuild(data, catalog, operations)
    except Problem as exc:
        check(exc.blockers is not None, "Problem 携带结构化 blockers")
        return exc
    raise AssertionError("预期重演被阻断，却返回了候选")


def print_set(blockers):
    print("    " + json.dumps(blockers["counts"], ensure_ascii=False))
    for rec in blockers["verified"]:
        print(f"    [{rec['id']}] 已验证 {rec['kind']} @ {rec['table']} {rec.get('key')}: {rec['message']}")
    for rec in blockers["unchecked"]:
        print(f"    [{rec['id']}] 未继续检查 @ {rec['table']} {rec.get('key')}（被 {rec['blocked_by']} 拦截）")
    for rec in blockers["gaps"]:
        fk = rec.get("foreign_key") or {}
        target = fk.get("key")
        print(f"    [{rec['id']}] 缺口 {rec['code']} @ {rec['table']} {rec.get('key')} "
              f"-> {fk.get('references')} 目标键 {target} 状态 {fk.get('target_state')}（关联 {rec.get('linked_to')}）")


def demonstrate_order_sensitivity():
    print("\n[0] 旧行为的次序敏感性（直接在全新库上按两种次序插入）")
    seed = {"team": [("t1", "一队")], "member": [(1, "one@x", 20, "t1")]}
    data, _ = make_ancestor(CORE_SCHEMA, seed)
    bad_check = (2, "bad-age@x", -3, "t1")
    bad_unique = (3, "one@x", 30, "t1")

    def first_error(order):
        db = sqlite3.connect(":memory:")
        db.deserialize(data)
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN")
        for row in order:
            try:
                cols = "id,email,age,team_code"
                marks = "?,?,?,?"
                db.execute(f"INSERT INTO member({cols}) VALUES ({marks})", row)
            except sqlite3.IntegrityError as exc:
                return str(exc)
        return None

    first_a = first_error([bad_check, bad_unique])
    first_b = first_error([bad_unique, bad_check])
    print(f"    次序 CHECK→UNIQUE 首项：{first_a}")
    print(f"    次序 UNIQUE→CHECK 首项：{first_b}")
    check(first_a != first_b and "CHECK" in first_a and "UNIQUE" in first_b,
          "不同输入次序确实触发了不同首项（问题背景）")


def demonstrate_independent():
    print("\n[1] 两个相互独立的约束组：CHECK 与 UNIQUE 一次收全")
    data, catalog = make_ancestor(CORE_SCHEMA, {
        "team": [("t1", "一队")],
        "member": [(1, "one@x", 20, "t1")],
        "assignment": [],
    })
    ops = {"member": [
        change("member", [2], [2, "bad-age@x", -3, "t1"]),
        change("member", [3], [3, "one@x", 30, "t1"]),
    ]}
    exc = blocked(data, catalog, ops)
    b = exc.blockers
    print_set(b)
    kinds = {rec["kind"] for rec in b["verified"]}
    check(kinds == {"check", "unique"}, f"同时验证出 check 与 unique（实际 {sorted(kinds)}）")
    check(len(b["verified"]) == 2 and len(b["unchecked"]) == 2, "两个失败条目都标为无法继续检查")
    check(exc.code == "constraint" and "CHECK" in exc.detail, "传统 problem 仍给出确定性的首要原因")

    reversed_ops = {"member": list(reversed(ops["member"]))}
    exc_reversed = blocked(data, catalog, reversed_ops)
    check(canonical(exc_reversed.blockers) == canonical(b),
          "颠倒输入次序后阻断集合（含稳定编号）完全一致")


def demonstrate_masking_and_recovery():
    print("\n[2] 掩盖组：同一 scope 字段先撞 CHECK，其外键错误被掩盖")
    data, catalog = make_ancestor(CORE_SCHEMA, {
        "team": [("t1", "一队")],
        "member": [],
        "assignment": [],
    })
    bad_ops = {"assignment": [change("assignment", [1], [1, "x"])]}
    exc = blocked(data, catalog, bad_ops)
    b = exc.blockers
    print_set(b)
    check([v["kind"] for v in b["verified"]] == ["check"], "首要且唯一已验证原因是 CHECK")
    check(b["counts"]["unchecked"] == 1, "失败行进入 unchecked")
    masked = [g for g in b["gaps"] if g["code"] == "masked_foreign_key"]
    check(len(masked) == 1, "被掩盖的外键作为缺口明确列出")
    gap = masked[0]
    check(gap["linked_to"] == b["verified"][0]["id"], "缺口回链到已验证的 CHECK 编号")
    fk = gap["foreign_key"]
    check(fk["columns"] == ["scope"] and fk["references"]["table"] == "team",
          "缺口定位到同一字段 scope 及其目标表")
    check(fk["target_state"] == "absent" and fk["key"] == [["t", "x"]],
          "按计划目标态确认缺失的父键是 'x'，使缺口可行动")

    print("\n[2a] 只修复 CHECK（scope='zz'，父行仍不存在）：缺口变成标准外键验证结果")
    partial = {"assignment": [change("assignment", [1], [1, "zz"])]}
    exc2 = blocked(data, catalog, partial)
    b2 = exc2.blockers
    print_set(b2)
    check(exc2.code == "foreign_key", "首要问题切换为 foreign_key")
    check(any(v["kind"] == "foreign_key" and v["key"] == [["i", "1"]] for v in b2["verified"]),
          "foreign_key_check 现在验证出该条目")
    check(b2["gaps"] == [] and b2["unchecked"] == [], "行成功写入后不再有掩盖缺口")

    print("\n[2b] 完全恢复（scope='t1' 指向已有父行）：同一重建入口返回通过候选")
    good = {"assignment": [change("assignment", [1], [1, "t1"])]}
    candidate = rebuild(data, catalog, good)
    probe = sqlite3.connect(":memory:")
    probe.deserialize(candidate)
    integrity = probe.execute("PRAGMA integrity_check(1)").fetchone()[0]
    fk_violations = probe.execute("PRAGMA foreign_key_check").fetchall()
    row = probe.execute("SELECT scope FROM assignment WHERE id=1").fetchone()
    probe.close()
    check(integrity == "ok" and fk_violations == [], "候选字节独立通过 integrity_check 与 foreign_key_check")
    check(row == ("t1",), "候选中实际重读的行就是恢复后的目标行")
    print("    ── 阻断 vs 恢复对比 ─────────────────────────────")
    print(f"    阻断版：status=blocked / problem={exc.code} / "
          f"verified={b['counts']['verified']} unchecked={b['counts']['unchecked']} gaps={b['counts']['gaps']}")
    print(f"    恢复版：status=passed  / candidate={len(candidate)} 字节 / "
          f"sha256={hashlib.sha256(candidate).hexdigest()[:16]}… / 行={row[0]}")


def demonstrate_without_rowid_identity():
    print("\n[3] WITHOUT ROWID 复合外键违规也能定位回复合主键")
    data, catalog = make_ancestor(WR_SCHEMA, {"parent": [("x", 1)], "child": []})
    ops = {"child": [change("child", ["z", 3], ["z", 3, "orphan"])]}
    exc = blocked(data, catalog, ops)
    fks = [v for v in exc.blockers["verified"] if v["kind"] == "foreign_key"]
    check(any(v["key"] == [["t", "z"], ["i", "3"]] for v in fks),
          f"反查出子行复合主键（实际 {[v['key'] for v in fks]}）")


# -- 第二部分：真实 HTTP 入口 ------------------------------------------------

HTTP_SCHEMA = CORE_SCHEMA  # 复用同一模式，构造三方镜像


def http_image(flavor):
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(HTTP_SCHEMA)
    db.executemany("INSERT INTO team VALUES(?,?)",
                   [("c1", "初名一"), ("c2", "初名二"), ("t9", "九队")])
    db.executemany("INSERT INTO member VALUES(?,?,?,?)", [
        (1, "one@example", 20, "c1"),
        (9, "nine@example", 40, "t9"),
    ])
    if flavor == "base":
        pass
    elif flavor == "left":
        db.execute("UPDATE team SET title='碰撞名称' WHERE code='c1'")
        db.execute("UPDATE team SET title='左安二' WHERE code='c2'")
        db.execute("UPDATE member SET email='nine-left@example' WHERE id=9")
    elif flavor == "right":
        db.execute("UPDATE team SET title='右安一' WHERE code='c1'")
        db.execute("UPDATE team SET title='碰撞名称' WHERE code='c2'")
        db.execute("DELETE FROM member WHERE id=9")
        db.execute("DELETE FROM team WHERE code='t9'")
    db.commit()
    data = db.serialize()
    db.close()
    return data


def demonstrate_http():
    print("\n[4] 真实 HTTP：一个计划同时暴露唯一键组与外键组，恢复后通过并封存下载")
    with tempfile.TemporaryDirectory(prefix="pagefold-blockers-") as root:
        server = make_server(Store(Path(root) / "state.sqlite"), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = _Client(server.server_port)
            base = client.upload("blk-base", http_image("base"))
            left = client.upload("blk-left", http_image("left"))
            right = client.upload("blk-right", http_image("right"))
            plan = client.call("/api/plans", {
                "base": base["id"], "left": left["id"], "right": right["id"]}, 201)
            route = f"/api/plans/{plan['id']}"
            conflicts = [item for item in plan["items"] if item["automatic"] is None]
            check(len(conflicts) == 3, f"计划有 3 项冲突（实际 {len(conflicts)}）")
            by_table_key = {(i["table"], json.dumps(i["key"])): i["id"] for i in conflicts}

            def decide(table, key_values, choice, revision):
                item_id = by_table_key[(table, json.dumps(encoded_key(key_values)))]
                return client.call(route + "/decisions",
                                   {"revision": revision, "item": item_id, "choice": choice})

            # 坏选择：c1=left 与 c2=right 制造重名；member 9=left 留下孤儿引用。
            revision = 0
            revision = decide("team", ["c1"], "left", revision)["revision"]
            revision = decide("team", ["c2"], "right", revision)["revision"]
            revision = decide("member", [9], "left", revision)["revision"]
            check(revision == 3, "三次不同裁决推进到第 3 版")

            blocked_result = client.call(route + "/rehearse", {"revision": 3})
            check(blocked_result["status"] == "blocked", "HTTP 重演状态为 blocked")
            blockers = blocked_result.get("blockers") or {}
            kinds = {v["kind"] for v in blockers.get("verified", [])}
            print_set(blockers)
            check({"unique", "foreign_key"} <= kinds,
                  f"HTTP 结果同时含 unique 与 foreign_key（实际 {sorted(kinds)}）")

            again = client.call(route + "/rehearse", {"revision": 3})
            check(again == blocked_result, "同版重演返回完全相同的缓存证据（同版结果解释不变）")
            attempts = client.call(route + "/attempts")
            check(any(a["revision"] == 3 and a.get("blockers") for a in attempts),
                  "blockers 随 attempt 进入 GET /attempts 历史")

            page = client.call(f"/plans/{plan['id']}")
            check("阻断集合".encode() in page
                  and "经过验证的原因".encode() in page
                  and "尚无法继续检查的条目".encode() in page
                  and "仍会影响结论的缺口".encode() in page,
                  "HTML 计划页渲染阻断集合的三类信息")

            # 恢复：两个团队标题错开，member 9 跟随右支删除。
            decide("team", ["c1"], "right", 3)
            decide("team", ["c2"], "left", 4)
            final = decide("member", [9], "right", 5)
            check(final["revision"] == 6, "恢复裁决推进到第 6 版")
            passed = client.call(route + "/rehearse", {"revision": 6})
            check(passed["status"] == "passed" and passed.get("candidate_digest"),
                  "恢复后第 6 版重演通过并给出候选摘要")
            print("    ── HTTP 阻断 vs 恢复对比 ───────────────────────")
            print(f"    第3版：{blocked_result['status']} / {blocked_result['problem']['code']} "
                  f"/ counts={blocked_result['blockers']['counts']}")
            print(f"    第6版：{passed['status']} / changed={passed['changed']} / "
                  f"size={passed['size']} / digest={passed['candidate_digest'][:16]}…")

            certificate = client.call(route + "/seal", {"revision": 6})
            check(certificate["candidate_digest"] == passed["candidate_digest"], "第 6 版封存凭证绑定通过候选")
            candidate = client.call(route + "/download")
            check(hashlib.sha256(candidate).hexdigest() == passed["candidate_digest"],
                  "下载字节摘要与通过结果一致")
            probe = sqlite3.connect(":memory:")
            probe.deserialize(candidate)
            check(probe.execute("PRAGMA foreign_key_check").fetchall() == [],
                  "下载候选独立外键检查为空")
            probe.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


class _Client(Client):
    """复用 walkthrough 的 JSON/HTTP 客户端，输出静默。"""


def main():
    demonstrate_order_sensitivity()
    demonstrate_independent()
    demonstrate_masking_and_recovery()
    demonstrate_without_rowid_identity()
    demonstrate_http()
    print("\n" + ("全部演示断言通过。" if not failures else f"存在 {len(failures)} 个失败断言：{failures}"))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
