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


def image(flavor):
    if flavor not in {"quartz", "cerulean", "umber", "stress-left", "stress-right",
                      "group-left", "group-right"}:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
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
        elif flavor == "group-left":
            db.execute("UPDATE bench SET label='玄石（左）' WHERE code='north'")
            db.execute("UPDATE node SET note='左支分组' WHERE bench='north' AND slot=1")
            db.execute("UPDATE bench SET label='砂层（左）' WHERE code='south'")
            db.execute("UPDATE node SET weight=3.5 WHERE bench='south' AND slot=1")
            db.execute("UPDATE binding SET bench='south' WHERE id=1")
        elif flavor == "group-right":
            db.execute("UPDATE bench SET label='玄石（右）' WHERE code='north'")
            db.execute("UPDATE node SET payload=x'ee11' WHERE bench='north' AND slot=1")
            db.execute("UPDATE bench SET label='砂层（右）' WHERE code='south'")
            db.execute("UPDATE node SET weight=4.5 WHERE bench='south' AND slot=1")
        db.commit()
        return db.serialize()
    finally:
        db.close()
