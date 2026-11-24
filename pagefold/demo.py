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
CREATE INDEX node_weight ON node(weight);
"""

BULK_FLAVORS = {"bulk-quartz": "base", "bulk-cerulean": "left", "bulk-umber": "right"}
BULK_BENCH = [("north", "玄石"), ("south", "砂层"), ("east", "晨岩"), ("west", "暮砂")]


def bulk_seed(db, flavor):
    db.executemany("INSERT INTO bench VALUES(?,?)", BULK_BENCH)
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
        ("north", slot, f"祖本节点 {slot:02d}", bytes([slot % 256]), float(slot))
        for slot in range(1, 46)])
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
        ("east", slot, f"东侧节点 {slot:02d}", None, float(slot))
        for slot in range(1, 11)])
    db.execute("INSERT INTO binding VALUES(1,'north',1)")
    branch = BULK_FLAVORS[flavor]
    if branch == "left":
        db.executemany("UPDATE bench SET label=? WHERE code=?",
                       [(f"左支更名 {code}", code) for code, _ in BULK_BENCH])
        db.executemany("UPDATE node SET note=? WHERE bench='north' AND slot=?",
                       [(f"左支覆写 {slot:02d}", slot) for slot in range(1, 46)])
        db.executemany("UPDATE node SET note=? WHERE bench='east' AND slot=?",
                       [(f"左支单边 {slot:02d}", slot) for slot in range(1, 6)])
    elif branch == "right":
        db.executemany("UPDATE bench SET label=? WHERE code=?",
                       [(f"右支更名 {code}", code) for code, _ in BULK_BENCH])
        db.executemany("UPDATE node SET payload=?,weight=? WHERE bench='north' AND slot=?",
                       [(bytes([255 - slot % 256]), slot + 0.25, slot) for slot in range(1, 46)])
        db.executemany("UPDATE node SET payload=? WHERE bench='east' AND slot=?",
                       [(bytes([slot]), slot) for slot in range(6, 11)])


def image(flavor):
    if flavor not in {"quartz", "cerulean", "umber", "stress-left", "stress-right", *BULK_FLAVORS}:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        if flavor in BULK_FLAVORS:
            bulk_seed(db, flavor)
        else:
            db.executemany("INSERT INTO bench VALUES(?,?)", [("north", "玄石"), ("south", "砂层")])
            db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
                ("north", 1, "祖本节点", b"\x00\x14", 1.5), ("south", 1, "稳定节点", b"", 3.0)])
            db.execute("INSERT INTO binding VALUES(1,'north',1)")
            if flavor == "cerulean":
                db.execute("UPDATE node SET note='左支覆写' WHERE bench='north'")
                db.execute("INSERT INTO node VALUES('south',2,'左支新增',NULL,4.0)")
            elif flavor == "umber":
                db.execute("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'")
                db.execute("UPDATE bench SET label='右支更名' WHERE code='south'")
            elif flavor.startswith("stress"):
                db.execute("INSERT INTO bench VALUES(?, '共享键值')", (flavor,))
        db.commit()
        return db.serialize()
    finally:
        db.close()
