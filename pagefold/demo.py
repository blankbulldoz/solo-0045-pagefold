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
                      "packet-base", "packet-left", "packet-right"}:
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
        elif flavor.startswith("packet-"):
            _packet_flavor(db, flavor)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def _packet_flavor(db, flavor):
    db.executemany("INSERT INTO bench VALUES(?,?)", [("west", "西层"), ("east", "东层")])
    db.execute("UPDATE node SET note='甲祖本' WHERE bench='north' AND slot=1")
    db.execute("UPDATE node SET note='丙祖本' WHERE bench='south' AND slot=1")
    db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
        ("north", 2, "乙祖本", b"\x02", 2.0), ("south", 2, "丁祖本", b"\x04", 4.0),
        ("west", 1, "戊只祖本", b"\x05", 5.0)])
    if flavor == "packet-base":
        return
    side = "左" if flavor == "packet-left" else "右"
    db.executemany("UPDATE node SET note=? WHERE bench=? AND slot=?",
                   [(f"甲{side}值", "north", 1), (f"乙{side}值", "north", 2),
                    (f"丙{side}值", "south", 1), (f"丁{side}值", "south", 2)])
    if flavor == "packet-left":
        db.execute("DELETE FROM node WHERE bench='west'")
        db.execute("UPDATE node SET payload=x'aa01' WHERE bench='south' AND slot=2")
    else:
        db.execute("UPDATE node SET weight=4.5 WHERE bench='south' AND slot=1")
        db.execute("UPDATE node SET payload=x'bb01' WHERE bench='south' AND slot=2")
