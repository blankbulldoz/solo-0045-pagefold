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

IMPACT_SCHEMA = """
CREATE TABLE ledger(
    code TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL,
    note TEXT, PRIMARY KEY(code,kind)
) WITHOUT ROWID;
CREATE TABLE journal(
    id TEXT PRIMARY KEY, title TEXT NOT NULL UNIQUE
) WITHOUT ROWID;
CREATE TABLE shelf(
    sku TEXT PRIMARY KEY, qty INTEGER NOT NULL CHECK(qty >= 0)
) WITHOUT ROWID;
"""

IMPACT_FLAVORS = ("impact-base", "impact-left", "impact-right")


def impact_image(flavor):
    if flavor not in IMPACT_FLAVORS:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(IMPACT_SCHEMA)
        # ledger('alpha','one')：双边同值插入（祖本缺席，左右相同）
        # ledger('beta','one')：冲突项，裁决保留祖本（选择祖本、无最终写入）
        # ledger('gamma','two')：双边同值删除（删除后键缺席）
        # ledger('delta','one')：左支单边修改（自动采纳左支）
        db.executemany("INSERT INTO ledger VALUES(?,?,?,?)", [
            ("beta", "one", 10, "祖本内容"),
            ("gamma", "two", 20, "待删除"), ("delta", "one", 30, "祖本备注")])
        db.executemany("INSERT INTO journal VALUES(?,?)", [
            ("j1", "共同日志"), ("j2", "稳定日志")])
        db.executemany("INSERT INTO shelf VALUES(?,?)", [("s1", 1), ("s2", 2)])
        if flavor == "impact-left":
            db.execute("DELETE FROM ledger WHERE code='gamma'")
            db.execute("INSERT INTO ledger VALUES('alpha','one',11,'双边同插')")
            db.execute("UPDATE ledger SET amount=31,note='左支修改' WHERE code='delta'")
            db.execute("UPDATE ledger SET note='左支冲突方案' WHERE code='beta'")
        elif flavor == "impact-right":
            db.execute("DELETE FROM ledger WHERE code='gamma'")
            db.execute("INSERT INTO ledger VALUES('alpha','one',11,'双边同插')")
            db.execute("UPDATE ledger SET note='右支冲突方案' WHERE code='beta'")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def image(flavor):
    if flavor in IMPACT_FLAVORS:
        return impact_image(flavor)
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
