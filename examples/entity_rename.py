"""Compare legacy row-by-row plans with explicit entity-key rename plans.

Run directly from the repository root:

    python3 -m examples.entity_rename

The drill uses only real Pagefold services and real SQLite bytes: images are
registered in a temporary state store, plans are adjudicated, rehearsed and
sealed, and the sealed candidate is opened independently to inspect the
composite foreign key.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from pagefold.attempts import rehearse
from pagefold.decisions import resolve_entity
from pagefold.images import register
from pagefold.plans import create_plan, read_plan
from pagefold.releases import seal
from pagefold.store import Store

PARENT_SCHEMA = """
CREATE TABLE account (
    tenant TEXT NOT NULL,
    login TEXT NOT NULL,
    uid TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    PRIMARY KEY(tenant, login)
) WITHOUT ROWID;
CREATE TABLE membership (
    id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    account_login TEXT NOT NULL,
    owner_uid TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY(tenant, account_login) REFERENCES account(tenant, login)
);
CREATE INDEX membership_owner ON membership(owner_uid);
"""


def image_bytes(accounts, memberships=(), schema=PARENT_SCHEMA):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema)
        db.executemany("INSERT INTO account VALUES(?,?,?,?)", accounts)
        db.executemany("INSERT INTO membership VALUES(?,?,?,?,?)", memberships)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def simple_image(rows):
    schema = "CREATE TABLE gadget(code TEXT PRIMARY KEY, uid TEXT NOT NULL UNIQUE, label TEXT);"
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(schema)
        db.executemany("INSERT INTO gadget VALUES(?,?,?)", rows)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def scenario_images():
    add_delete_base = simple_image([("gone", "uid-gone", "即将删除"), ("keep", "uid-keep", "保留")])
    add_delete_left = simple_image([("keep", "uid-keep", "保留"), ("new", "uid-new", "新增")])
    add_delete_right = add_delete_base

    divergent_base = image_bytes(
        [("t", "alpha", "uid-a", "A")],
        [(1, "t", "alpha", "uid-a", "主成员")])
    divergent_left = image_bytes(
        [("t", "alpha-l", "uid-a", "A 左改名")],
        [(1, "t", "alpha-l", "uid-a", "主成员")])
    divergent_right = image_bytes(
        [("t", "alpha-r", "uid-a", "A 右改名")],
        [(1, "t", "alpha-r", "uid-a", "主成员")])

    preempt_base = image_bytes([("t", "alpha", "uid-a", "A")])
    preempt_left = image_bytes([("t", "candidate", "uid-a", "A 改名")])
    preempt_right = image_bytes([
        ("t", "alpha", "uid-a", "A"),
        ("t", "candidate", "uid-c", "抢占者 C"),
    ])

    swap_base = image_bytes(
        [("t", "alpha", "uid-a", "A"), ("t", "beta", "uid-b", "B")],
        [(1, "t", "alpha", "uid-a", "A 的引用"),
         (2, "t", "beta", "uid-b", "B 的引用")])
    swap_left = image_bytes(
        [("t", "beta", "uid-a", "A"), ("t", "alpha", "uid-b", "B")],
        [(1, "t", "beta", "uid-a", "A 的引用"),
         (2, "t", "alpha", "uid-b", "B 的引用")])
    swap_right = swap_base

    return {
        "add_delete": (add_delete_base, add_delete_left, add_delete_right, "gadget", ["uid"]),
        "divergent_rename": (divergent_base, divergent_left, divergent_right, "account", ["uid"]),
        "preempted_candidate": (preempt_base, preempt_left, preempt_right, "account", ["uid"]),
        "mutual_key_swap": (swap_base, swap_left, swap_right, "account", ["uid"]),
    }


def register_sources(store, trio):
    base, left, right, table, _ = trio
    return {role: register(store, data, f"{table}-{role}")["id"]
            for role, data in (("base", base), ("left", left), ("right", right))}


def get_plan(store, plan_id):
    with store.connect() as db:
        return read_plan(db, plan_id)


def row_mode_summary(plan):
    tables = Counter(item["table"] for item in plan["items"])
    return {"items": len(plan["items"]), "unresolved": plan["unresolved"], "tables": dict(tables)}


def entity_mode_summary(plan):
    kinds = Counter(entity["kind"] for entity in plan["entities"])
    return {"entities": len(plan["entities"]), "items": len(plan["items"]),
            "unresolved": plan["unresolved"], "kinds": dict(kinds),
            "renames": sum(1 for entity in plan["entities"] if entity["rename"])}


def decide_all_automatic_entities(store, plan):
    revision = plan["revision"]
    for entity in plan["entities"]:
        if not entity["explicit"] and entity["choice"] in ("left", "right", "base"):
            result = resolve_entity(store, plan["id"], revision, entity["id"],
                                    data={"choice": entity["choice"]})
            revision = result["revision"]
    return get_plan(store, plan["id"])


def print_compare(title, row_plan, entity_plan):
    print(f"\n== {title} ==")
    print("逐行模式:", row_mode_summary(row_plan))
    print("实体模式:", entity_mode_summary(entity_plan))
    print("逐行条目:")
    for item in row_plan["items"]:
        print(" ", item["table"], item["key"], "auto=", item["automatic"])
    print("实体组:")
    for entity in entity_plan["entities"]:
        print(" ", entity["entity_key"], entity["kind"], entity["status"], "keys=", entity["keys"])


def verify_sealed_composite_fk(store, plan_id, revision):
    certificate = seal(store, plan_id, revision)
    with store.connect() as db:
        candidate = db.execute("SELECT candidate FROM releases WHERE plan_id=?", (plan_id,)).fetchone()[0]
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(candidate)
        fk_list = connection.execute("PRAGMA foreign_key_list(membership)").fetchall()
        assert len(fk_list) == 2, f"不是复合外键: {fk_list!r}"
        assert {row[3] for row in fk_list} == {"tenant", "account_login"}
        assert {row[4] for row in fk_list} == {"tenant", "login"}
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"封存件存在外键违规: {violations!r}"
        semantic = connection.execute("""
            SELECT m.id, a.uid, m.owner_uid
            FROM membership m JOIN account a
              ON m.tenant = a.tenant AND m.account_login = a.login
            ORDER BY m.id
        """).fetchall()
        assert [(row[0], row[1], row[2]) for row in semantic] == [
            (1, "uid-a", "uid-a"), (2, "uid-b", "uid-b")], semantic
        accounts = connection.execute("SELECT login, uid FROM account ORDER BY uid").fetchall()
        assert accounts == [("beta", "uid-a"), ("alpha", "uid-b")], accounts
    finally:
        connection.close()
    assert certificate["entity_mode"] is True
    assert certificate["revision"] == revision
    return certificate, len(candidate)


def run():
    images = scenario_images()
    with tempfile.TemporaryDirectory(prefix="pagefold-entity-") as directory:
        store = Store(Path(directory) / "entity.sqlite")

        # Add/delete: stable keys prove the insert and delete are two entities.
        trio = images["add_delete"]
        sources = register_sources(store, trio)
        row_plan = create_plan(store, sources)
        entity_plan = create_plan(store, sources, {"entity_keys": [{"table": trio[3], "columns": trio[4]}]})
        print_compare("新增/删除", row_plan, entity_plan)
        assert len(row_plan["items"]) == 2
        assert entity_plan["unresolved"] == 0
        assert {entity["kind"] for entity in entity_plan["entities"]} == {"insert", "delete"}

        # Divergent renames: row mode sees old delete plus two candidate inserts
        # and an unrelated child-FK conflict. Entity mode has one decision.
        trio = images["divergent_rename"]
        sources = register_sources(store, trio)
        row_plan = create_plan(store, sources)
        entity_plan = create_plan(store, sources, {"entity_keys": {"account": ["uid"]}})
        print_compare("左右支改成两个键", row_plan, entity_plan)
        assert len(row_plan["items"]) == 4
        assert row_plan["unresolved"] == 1  # membership composite-FK text conflict
        assert entity_plan["unresolved"] == 1
        assert len(entity_plan["entities"]) == 1
        entity = entity_plan["entities"][0]
        try:
            resolve_entity(store, entity_plan["id"], 0, entity["id"],
                           data={"choice": "accept"})
        except Exception as exc:
            assert getattr(exc, "code", None) == "rename_side_required"
        else:
            raise AssertionError("两个不同候选键时不应允许无侧别 accept")
        accepted = resolve_entity(store, entity_plan["id"], 0, entity["id"],
                                  data={"decision": "accept", "side": "left"})
        assert accepted["revision"] == 1 and accepted["status"] == "accepted"
        passed = rehearse(store, entity_plan["id"], 1)
        assert passed["status"] == "passed", passed
        assert passed["reference_rows_updated"] == 1

        # The old row-by-row plan cannot express "only one rename": both
        # branch-only inserts are automatic and violate uid UNIQUE after the
        # child conflict is resolved.
        child = next(item for item in row_plan["items"] if item["table"] == "membership")
        from pagefold.decisions import resolve
        resolve(store, row_plan["id"], 0, child["id"], "left")
        blocked = rehearse(store, row_plan["id"], 1)
        assert blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint", blocked

        # Candidate-key preemption: explicit accept remains visible after a
        # blocked rehearsal; explicit rejection then yields a passing candidate.
        trio = images["preempted_candidate"]
        sources = register_sources(store, trio)
        row_plan = create_plan(store, sources)
        collision_plan = create_plan(store, sources, {"entity_keys": [{"table": "account", "columns": ["uid"]}]})
        collision_plan = decide_all_automatic_entities(store, collision_plan)
        print_compare("另一行抢占候选键", row_plan, collision_plan)
        assert any(entity["candidate_conflicts"] for entity in collision_plan["entities"])
        blocked = rehearse(store, collision_plan["id"], collision_plan["revision"])
        assert blocked["status"] == "blocked" and blocked["problem"]["code"] == "constraint", blocked
        occupant = next(entity for entity in collision_plan["entities"]
                        if entity["entity_key"][0][1] == "uid-c")
        rejected = resolve_entity(store, collision_plan["id"], collision_plan["revision"],
                                  occupant["id"], data={"decision": "reject"})
        passed = rehearse(store, collision_plan["id"], rejected["revision"])
        assert passed["status"] == "passed", passed
        persisted = get_plan(store, collision_plan["id"])
        statuses = {entity["entity_key"][0][1]: entity["status"]
                    for entity in persisted["entities"]}
        assert statuses == {"uid-a": "accepted", "uid-c": "rejected"}, statuses

        # Mutual key occupation / swap with a real sealed composite FK.
        trio = images["mutual_key_swap"]
        sources = register_sources(store, trio)
        row_swap = create_plan(store, sources)
        swap_plan = create_plan(store, sources, {"entity_keys": [{"table": "account", "columns": ["uid"]}]})
        print_compare("相互占键/交换主键", row_swap, swap_plan)
        assert len(swap_plan["entities"]) == 2
        swap_plan = decide_all_automatic_entities(store, swap_plan)
        assert swap_plan["unresolved"] == 0
        passed = rehearse(store, swap_plan["id"], swap_plan["revision"])
        assert passed["status"] == "passed", passed
        assert passed["reference_rows_updated"] == 2, passed
        certificate, size = verify_sealed_composite_fk(store, swap_plan["id"], swap_plan["revision"])
        print("\n封存复合外键 ✓ revision", certificate["revision"],
              "candidate", certificate["candidate_digest"][:16], "bytes", size)
        print("\n全部实体改名对比与封存检查通过。")


def main():
    run()


if __name__ == "__main__":
    main()
