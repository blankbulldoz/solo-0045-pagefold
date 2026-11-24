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


LINEAGE_FLAVORS = {
    # 第一代：封存成果 C1 与两个二代分支的共同祖本。
    "gen1-base", "gen1-left", "gen1-right",
    # 第二代：从封存成果继续分叉的两条支线。
    "gen2-left", "gen2-right",
    # 文件名沿用旧称、字节却已改写的误导性镜像。
    "renamed",
    # 交接清单里完全查不到出处的镜像，字节独立，避免与改名陷阱塌缩成同一摘要。
    "mystery",
}


def lineage_image(flavor):
    if flavor not in LINEAGE_FLAVORS:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO bench VALUES(?,?)", [("north", "玄石"), ("south", "砂层")])
        db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
            ("north", 1, "祖本节点", b"\x00\x14", 1.5), ("south", 1, "稳定节点", b"", 3.0)])
        db.execute("INSERT INTO binding VALUES(1,'north',1)")
        if flavor == "gen1-left":
            db.execute("UPDATE node SET note='左支覆写' WHERE bench='north'")
        elif flavor == "gen1-right":
            db.execute("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'")
        elif flavor == "gen2-left":
            # 在一代封存结果上继续改动，形成二代左支。
            db.execute("UPDATE node SET note='左支覆写' WHERE bench='north'")
            db.execute("INSERT INTO node VALUES('south',2,'二代左支新增',NULL,4.0)")
        elif flavor == "gen2-right":
            db.execute("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'")
            db.execute("UPDATE bench SET label='二代右支更名' WHERE code='south'")
        elif flavor == "renamed":
            # 同名陷阱：登记标签会写成 gen1-right，但内容是另一套改写。
            db.execute("UPDATE node SET note='改名陷阱节点' WHERE bench='north'")
        elif flavor == "mystery":
            db.execute("UPDATE node SET note='出处不明节点' WHERE bench='north'")
            db.execute("UPDATE bench SET label='孤证更名' WHERE code='south'")
        db.commit()
        return db.serialize()
    finally:
        db.close()
