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

PARTIAL_SCHEMA = """
CREATE TABLE permit(
    code TEXT PRIMARY KEY, holder TEXT NOT NULL, region TEXT,
    active INTEGER NOT NULL CHECK(active IN (0,1))
) WITHOUT ROWID;
CREATE UNIQUE INDEX permit_live ON permit(holder, region) WHERE active;
"""

PARTIAL_FLAVORS = ("partial-base", "partial-left", "partial-right")


def image(flavor):
    if flavor in PARTIAL_FLAVORS:
        return partial_image(flavor)
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


def partial_image(flavor):
    """部分唯一索引示例：祖本与右支各自合法，裁决保留祖本后与左支新行碰撞。

    p-east 在祖本中是活跃行；左支将其停用并新增同值的活跃行 p-znew，
    右支将其改名。裁决保留祖本时，p-znew 的写入与未变祖本行 p-east
    在谓词索引 permit_live 下碰撞。p-old 值相同但不满足谓词，p-null
    的 region 为 NULL，两者恰好说明为何看起来值相同的行却通过。
    """
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(PARTIAL_SCHEMA)
        db.executemany("INSERT INTO permit VALUES(?,?,?,?)", [
            ("p-east", "shared", "east", 1), ("p-old", "shared", "east", 0)])
        if flavor == "partial-left":
            db.execute("UPDATE permit SET active=0 WHERE code='p-east'")
            db.execute("INSERT INTO permit VALUES('p-null','shared',NULL,1)")
            db.execute("INSERT INTO permit VALUES('p-znew','shared','east',1)")
        elif flavor == "partial-right":
            db.execute("UPDATE permit SET holder='renamed' WHERE code='p-east'")
        db.commit()
        return db.serialize()
    finally:
        db.close()
