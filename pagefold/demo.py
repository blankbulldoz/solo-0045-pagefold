import sqlite3


SCHEMA = """
CREATE TABLE bench(code TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE TABLE node(
    bench TEXT NOT NULL REFERENCES bench(code), slot INTEGER NOT NULL,
    note TEXT, payload BLOB, weight REAL CHECK(weight >= 0),
    PRIMARY KEY(bench,slot)
) WITHOUT ROWID;
CREATE TABLE binding(id INTEGER PRIMARY KEY, bench TEXT, slot INTEGER,
    FOREIGN KEY(bench,slot) REFERENCES node(bench,slot));
CREATE TABLE ledger(entry TEXT PRIMARY KEY, amount INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE alias(code TEXT PRIMARY KEY, tag TEXT NOT NULL UNIQUE) WITHOUT ROWID;
CREATE INDEX node_weight ON node(weight);
"""

FLAVORS = {"quartz", "cerulean", "umber", "stress-left", "stress-right",
           "cedar-left", "cedar-right"}


def seed(db):
    db.executemany("INSERT INTO bench VALUES(?,?)", [("north", "玄石"), ("south", "砂层")])
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
        ("north", 1, "祖本节点", b"\x00\x14", 1.5), ("south", 1, "稳定节点", b"", 3.0)])
    db.executemany("INSERT INTO binding VALUES(?,?,?)",
                   [(1, "north", 1), (2, "south", 1)])
    db.executemany("INSERT INTO ledger VALUES(?,?)", [("opening", 100), ("reserve", 7)])
    db.executemany("INSERT INTO alias VALUES(?,?)", [("north", "北极"), ("south", "南针")])


def image(flavor):
    if flavor not in FLAVORS:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        seed(db)
        if flavor == "cerulean":
            db.execute("UPDATE node SET note='左支覆写' WHERE bench='north'")
            db.execute("INSERT INTO node VALUES('south',2,'左支新增',NULL,4.0)")
        elif flavor == "umber":
            db.execute("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'")
            db.execute("UPDATE bench SET label='右支更名' WHERE code='south'")
        elif flavor.startswith("stress"):
            # bench 名称两支不同（收窄到 bench 可成功）；alias 标签两支同名（必须继续收窄）。
            db.execute("INSERT INTO bench VALUES(?, ?)", (flavor, f"{flavor} 名称"))
            db.execute("INSERT INTO alias VALUES(?, '同名标签')", (flavor,))
        elif flavor.startswith("cedar"):
            # 两支共同移除 node('north',1)，并把 bench 'north' 替换为 'gale'。
            # 先把复合外键指向的 binding(1) 改挂到仍然存在的 south 节点，保持分支自身有效。
            db.execute("UPDATE binding SET bench='south',slot=1 WHERE id=1")
            db.execute("DELETE FROM node WHERE bench='north' AND slot=1")
            db.execute("DELETE FROM bench WHERE code='north'")
            db.execute("INSERT INTO bench VALUES('gale','风槽')")
            if flavor == "cedar-left":
                # 左支进一步删除改挂的 binding，并补上引用 gale 的节点。
                db.execute("DELETE FROM binding WHERE id=1")
                db.execute("INSERT INTO node VALUES('gale',1,'左支风节点',NULL,2.0)")
            else:
                # 右支保留改挂的 binding(1)（仍指向 south），binding 表在两支间不再有差异。
                pass
        db.commit()
        return db.serialize()
    finally:
        db.close()
