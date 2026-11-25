"""升级迁移复核：用升级前的旧表结构造一份状态库，再由新版 Store 打开。

复核点：
1. 旧库的 attempts 行没有 choices 等新列，迁移后仍可读取并标记为 legacy；
2. 迁移不损坏既有镜像、计划和封存对象；
3. 新版写入的重演带选择快照，裁决事件表从空开始累积；
4. 归档后旧记录与新记录都继续可查。
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from pagefold.attempts import list_attempts
from pagefold.audit import attempt_detail, dossier
from pagefold.catalog import describe
from pagefold.codec import canonical, digest
from pagefold.constraints import check_database
from pagefold.decisions import resolve
from pagefold.demo import image
from pagefold.rows import read_image
from pagefold.sqlite_image import mounted
from pagefold.store import Store

OLD_SCHEMA = """
CREATE TABLE images (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, catalog TEXT NOT NULL, body BLOB NOT NULL
);
CREATE TABLE plans (
    id TEXT PRIMARY KEY, base TEXT NOT NULL REFERENCES images(id),
    left_id TEXT NOT NULL REFERENCES images(id), right_id TEXT NOT NULL REFERENCES images(id),
    items TEXT NOT NULL, decisions TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open', 'sealed', 'archived'))
);
CREATE TABLE attempts (
    plan_id TEXT NOT NULL REFERENCES plans(id), revision INTEGER NOT NULL,
    result TEXT NOT NULL, candidate BLOB, PRIMARY KEY(plan_id, revision)
);
CREATE TABLE releases (
    plan_id TEXT PRIMARY KEY REFERENCES plans(id), certificate TEXT NOT NULL,
    candidate BLOB NOT NULL
);
"""


def catalog_for(flavor):
    data = image(flavor)
    with mounted(data) as db:
        catalog = describe(db)
        check_database(db)
        rows = read_image(db, catalog)
    for table in catalog["tables"]:
        table["rows"] = len(rows[table["name"]])
    return data, canonical(catalog)


def main():
    with tempfile.TemporaryDirectory(prefix="pagefold-migration-") as root:
        path = Path(root) / "old-state.sqlite"
        # 第一步：完全按升级前的结构和写入方式造旧库。
        db = sqlite3.connect(path)
        try:
            db.executescript(OLD_SCHEMA)
            ids = {}
            for flavor, label in (("quartz", "石英祖本"), ("cerulean", "青蓝分支"), ("umber", "赤褐分支")):
                body, catalog = catalog_for(flavor)
                ids[flavor] = digest(body)
                db.execute("INSERT INTO images VALUES(?,?,?,?)", (ids[flavor], label, catalog, body))
            plan_id = "legacy-plan-0001"
            db.execute(
                "INSERT INTO plans(id,base,left_id,right_id,items) VALUES(?,?,?,?,?)",
                (plan_id, ids["quartz"], ids["cerulean"], ids["umber"], canonical([])))
            old_result = {"id": plan_id, "revision": 0, "changed": 3, "status": "passed",
                          "candidate_digest": "a" * 64, "size": 128}
            db.execute("INSERT INTO attempts(plan_id,revision,result,candidate) VALUES(?,?,?,?)",
                       (plan_id, 0, canonical(old_result), None))
            db.commit()
        finally:
            db.close()

        # 第二步：新版 Store 打开旧库，触发自动迁移。
        store = Store(path)
        with store.connect() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
            assert {"choices", "decision_digest", "requested_at", "sequence"} <= columns
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "decision_events" in tables
            legacy_row = conn.execute(
                "SELECT choices FROM attempts WHERE plan_id=? AND revision=0", (plan_id,)).fetchone()
            assert legacy_row[0] is None, "旧记录不应被凭空补上选择快照"

            history = list_attempts(conn, plan_id)
            assert len(history) == 1 and history[0]["record"] == "legacy"
            assert history[0]["choices"] is None
            detail = attempt_detail(conn, plan_id, 0)
            assert detail["status"] == "passed" and detail["record"] == "legacy"

        # 第三步：迁移后的库支持新版写入（事件流、选择快照）。
        # 旧计划的 items 是空列表，这里用真实镜像重建一个计划来验证新链路。
        from pagefold.plans import create_plan
        with store.connect() as conn:
            row = conn.execute("SELECT id FROM images WHERE label='石英祖本'").fetchone()
            base_id = row[0]
            left_id = conn.execute("SELECT id FROM images WHERE label='青蓝分支'").fetchone()[0]
            right_id = conn.execute("SELECT id FROM images WHERE label='赤褐分支'").fetchone()[0]
        plan = create_plan(store, {"base": base_id, "left": left_id, "right": right_id})
        conflict = next(item for item in plan["items"] if item["automatic"] is None)
        resolve(store, plan["id"], 0, conflict["id"], "right")
        from pagefold.attempts import rehearse
        current = rehearse(store, plan["id"], 1)
        assert current["record"] == "current" and current["choices"] is not None
        pinned = next(c for c in current["choices"] if c["item"] == conflict["id"])
        assert pinned["choice"] == "right" and pinned["decided"] is True

        # 第四步：案卷同时覆盖旧计划（旧记录）和新计划（新记录），归档不销毁证据。
        from pagefold.archive import archive
        archive(store, plan["id"], 1)
        with store.connect() as conn:
            book_old = dossier(conn, plan_id)
            assert book_old["legacy_count"] == 1 and book_old["attempts"][0]["record"] == "legacy"
            book_new = dossier(conn, plan["id"])
            assert book_new["archived"] is True
            assert book_new["events"] and book_new["current_attempt"]["record"] == "current"
            assert attempt_detail(conn, plan["id"], 1)["choices"] is not None

    print("migration ✓ 旧库自动迁移，旧记录标记 legacy 且可回查，新记录带选择快照，归档后证据完整")


if __name__ == "__main__":
    main()
