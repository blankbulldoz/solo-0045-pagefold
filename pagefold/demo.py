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


IDENTITY_SCHEMA = """
CREATE TABLE reading(
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    measure,
    payload BLOB
);
"""


def identity_base():
    """三行灵活类型列样本：包含整数、NULL 与非空 BLOB。"""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(IDENTITY_SCHEMA)
        db.executemany("INSERT INTO reading VALUES(?,?,?,?)", [
            (1, "alpha", 42, None),
            (2, "beta", 7, b"\x01"),
            (3, "gamma", 99, b"\x02\x03"),
        ])
        db.commit()
        return db.serialize()
    finally:
        db.close()


def vacuum_copy(data):
    """制造碎片后 VACUUM：目录与类型化内容不变，文件字节几乎必然改变。"""
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.deserialize(data)
        db.execute("CREATE TABLE IF NOT EXISTS _churn(id INTEGER PRIMARY KEY, pad TEXT)")
        db.executemany("INSERT INTO _churn VALUES(?,?)",
                       [(index, "x" * 40) for index in range(1500)])
        db.execute("DROP TABLE _churn")
        db.execute("VACUUM")
        return db.serialize()
    finally:
        db.close()


def type_shifted():
    """同模式、同行数、同行位置，但整数变文本、NULL 变空 BLOB。"""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(IDENTITY_SCHEMA)
        db.executemany("INSERT INTO reading VALUES(?,?,?,?)", [
            (1, "alpha", "42", b""),
            (2, "beta", "7", b"\x01"),
            (3, "gamma", "99", b"\x02\x03"),
        ])
        db.commit()
        return db.serialize()
    finally:
        db.close()
