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
CREATE TABLE ledger(
    site TEXT NOT NULL COLLATE BINARY, marker NOT NULL, label TEXT NOT NULL,
    PRIMARY KEY(site,marker)
) WITHOUT ROWID;
CREATE INDEX node_weight ON node(weight);
"""

# ledger 用来演练类型化主键查找：大小写不同的文本、整数与等值实数、空 BLOB。
LEDGER = ([
    ("Alpha", b"", "空二进制键"), ("Alpha", "", "空文本键"), ("Alpha", 2.0, "整数等值实数"),
    ("Alpha", 2.5, "实数键"), ("Beta", 3, "整数键"), ("alpha", 1, "小写文本键"),
    ("alpha", b"", "小写空二进制键"),
] + [("north", number, f"节点 {number}") for number in range(1, 141) if number != 7])


def image(flavor):
    if flavor not in {"quartz", "cerulean", "umber", "stress-left", "stress-right"}:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO bench VALUES(?,?)", [("north", "玄石"), ("south", "砂层")])
        db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
            ("north", 1, "祖本节点", b"\x00\x14", 1.5), ("south", 1, "稳定节点", b"", 3.0)])
        db.execute("INSERT INTO binding VALUES(1,'north',1)")
        db.executemany("INSERT INTO ledger VALUES(?,?,?)", LEDGER)
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
