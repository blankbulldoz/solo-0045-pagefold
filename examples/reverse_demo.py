"""已封存合流逆向包的发放与接收演示。

第一阶段在真实服务内完成合流并封存；之后全程脱离服务：
发放方离线签发逆向包，接收方对三种目标库（原样、部分已还原、
有后续依赖）做预检与原子应用，并证明失败尝试不改动目标库。
"""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from threading import Thread

from examples.walkthrough import Client, expect
from pagefold.catalog import describe
from pagefold.codec import canonical, digest
from pagefold.demo import image
from pagefold.reverse import apply as apply_reverse
from pagefold.reverse import dump_package, issue, load_package, preflight, render_report
from pagefold.rows import read_image
from pagefold.server import make_server
from pagefold.sqlite_image import mounted
from pagefold.store import Store

NODE_NORTH_1 = [["t", "north"], ["i", "1"]]


def snapshot(data):
    with mounted(data) as db:
        return read_image(db, describe(db))


def mutate(data, *statements):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        db.execute("PRAGMA foreign_keys=ON")
        for sql, params in statements:
            db.execute(sql, params)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def row_of(data, table, key):
    return snapshot(data)[table][canonical(key)]


def seal_merge(root):
    """走真实 HTTP 入口完成一次合流并封存，返回候选字节与凭证。"""
    with make_server(Store(root / "state.sqlite"), 0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            client = Client(server.server_port)
            sources = {role: client.upload(name)["id"]
                       for role, name in (("base", "quartz"), ("left", "cerulean"), ("right", "umber"))}
            plan = client.call("/api/plans", sources, 201)
            route = f"/api/plans/{plan['id']}"
            conflict = next(item for item in plan["items"] if item["automatic"] is None)
            client.call(route + "/decisions", {"item": conflict["id"], "choice": "right", "revision": 0})
            client.call(route + "/rehearse", {"revision": 1})
            certificate = client.call(route + "/seal", {"revision": 1})
            candidate = client.call(route + "/download")
        finally:
            server.shutdown()
            thread.join()
    expect(digest(candidate) == certificate["candidate_digest"], "封存候选与凭证摘要不一致")
    return candidate, certificate


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-reverse-") as root:
        root = Path(root)

        print("── 发放：服务内合流并封存 ──")
        candidate, certificate = seal_merge(root)
        print(f"封存计划 {certificate['plan']} 版本 {certificate['revision']}，"
              f"候选摘要 {certificate['candidate_digest'][:16]}…")

        print("── 发放：离线签发逆向包（不接触服务） ──")
        package = issue(image("quartz"), candidate, certificate)
        package_path = root / "reverse-package.json"
        package_path.write_text(dump_package(package), encoding="utf-8")
        received = load_package(package_path.read_text(encoding="utf-8"))
        expect(received == package, "接收方载入的逆向包与签发不一致")
        print(f"逆向包 {package['digest'][:16]}…，{len(package['entries'])} 条确实修改的行，"
              "接收方摘要校验通过")

        print("── 接收：目标一，原样目标 ──")
        target1 = root / "target-pristine.sqlite"
        target1.write_bytes(candidate)
        report1 = preflight(received, target1.read_bytes())
        print(render_report(report1))
        expect(report1["feasible"] and report1["summary"] == {"revert": 3, "already_base": 0, "conflict": 0},
               "原样目标应全部待还原且整份可行")
        result1 = apply_reverse(received, target1)
        expect(result1["applied"], "原样目标应用失败")
        expect(snapshot(target1.read_bytes()) == snapshot(image("quartz")),
               "原样目标还原后应回到祖本内容")
        print("原样目标 ✓ 三行全部还原，库内容回到祖本")

        print("── 接收：目标二，部分已还原且复合键行被对方改写 ──")
        target2 = root / "target-partial.sqlite"
        target2.write_bytes(mutate(
            candidate,
            ("DELETE FROM node WHERE bench='south' AND slot=2", ()),           # 已被还原
            ("UPDATE node SET note='对方批注' WHERE bench='north' AND slot=1", ()),  # 复合键行被改写
            ("INSERT INTO bench VALUES('east','对方新增')", ())))                # 无关新行
        report2 = preflight(received, target2.read_bytes())
        print(render_report(report2))
        expect(report2["feasible"] and report2["summary"] == {"revert": 1, "already_base": 1, "conflict": 1},
               "部分已还原目标的三态统计不符")
        conflict = next(entry for entry in report2["entries"] if entry["status"] == "conflict")
        expect(conflict["table"] == "node" and conflict["key"] == NODE_NORTH_1,
               "被改写的复合键行未被单独标出")
        result2 = apply_reverse(received, target2)
        expect(result2["applied"] and result2["protected"] == [conflict["id"]], "目标二应用结果不符")
        after2 = snapshot(target2.read_bytes())
        expect(row_of(target2.read_bytes(), "node", NODE_NORTH_1)[2] == ["t", "对方批注"],
               "对方改写的复合键行应原样保留")
        expect(after2["node"].get(canonical([["t", "south"], ["i", "2"]])) is None,
               "已还原的行不应被补回")
        expect(row_of(target2.read_bytes(), "bench", [["t", "south"]]) == [["t", "south"], ["t", "砂层"]],
               "仍是合流值的行应被还原")
        expect(canonical([["t", "east"]]) in after2["bench"], "对方新增的无关行应保留")
        print("部分已还原目标 ✓ 复合键行区分出“对方改写”与“本是祖本”，对方写入全部保留")

        strict_copy = root / "target-strict.sqlite"
        strict_copy.write_bytes(target2.read_bytes())
        before = hashlib.sha256(strict_copy.read_bytes()).hexdigest()
        refused = apply_reverse(received, strict_copy, strict=True)
        expect(not refused["applied"] and refused["error"]["code"] == "conflict", "严格模式应拒绝冲突")
        expect(hashlib.sha256(strict_copy.read_bytes()).hexdigest() == before, "严格模式失败改动了目标库")
        print("严格模式 ✓ 遇对方改写整体拒绝，目标库字节未变")

        print("── 接收：目标三，后续外键依赖指向待删行 ──")
        target3 = root / "target-dependent.sqlite"
        dependent = mutate(candidate, ("INSERT INTO binding VALUES(2,'south',2)", ()))
        target3.write_bytes(dependent)
        report3 = preflight(received, target3.read_bytes())
        print(render_report(report3))
        expect(not report3["feasible"] and len(report3["blockers"]) == 1, "后续依赖未被预检拦截")
        blocker = report3["blockers"][0]
        expect(blocker["kind"] == "referenced" and blocker["referrer"]["table"] == "binding",
               "阻塞应归因到包外 binding 行")
        before = hashlib.sha256(target3.read_bytes()).hexdigest()
        result3 = apply_reverse(received, target3)
        expect(not result3["applied"] and result3["error"]["code"] == "blocked", "依赖目标应应用失败")
        expect(hashlib.sha256(target3.read_bytes()).hexdigest() == before, "失败尝试改动了目标库字节")
        expect(snapshot(target3.read_bytes()) == snapshot(dependent), "失败尝试改动了目标库内容")
        print("后续依赖目标 ✓ 整份判定不可行，失败尝试后目标库字节与内容均未变")

        print("── 候选间依赖：合流新增行互为外键时的分批 ──")
        staged = mutate(image("quartz"),
                        ("INSERT INTO bench VALUES('east','东支')", ()),
                        ("INSERT INTO node VALUES('east',1,'新节点',NULL,0.5)", ()))
        with mounted(staged) as db:
            signature = describe(db)["signature"]
        offline_certificate = {"plan": "offline-demo", "revision": 0,
                               "sources": {"base": digest(image("quartz"))},
                               "candidate_digest": digest(staged), "schema": signature}
        package2 = issue(image("quartz"), staged, offline_certificate)
        report4 = preflight(package2, staged)
        print(render_report(report4))
        expect(len(report4["dependencies"]) == 1 and len(report4["batches"]) == 2,
               "互为外键的删除应分成两批")
        first, second = (batch[0] for batch in report4["batches"])
        tables_by_id = {entry["id"]: entry["table"] for entry in report4["entries"]}
        expect(tables_by_id[first] == "node" and tables_by_id[second] == "bench",
               "应先删引用方 node 再删被引用方 bench")
        target4 = root / "target-staged.sqlite"
        target4.write_bytes(staged)
        expect(apply_reverse(package2, target4)["applied"], "分批应用失败")
        expect(snapshot(target4.read_bytes()) == snapshot(image("quartz")), "分批应用后应回到祖本内容")
        print("分批依赖 ✓ 引用方先行、被引用方随后，两批应用后回到祖本")

        print("reverse ✓ 发放、三种目标接收、失败不改动、候选依赖分批全部验证通过")


if __name__ == "__main__":
    main()
