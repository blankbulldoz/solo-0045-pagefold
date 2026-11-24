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


def witness_image(flavor):
    """含复合外键、NULL 半截键、循环与大量入向引用的对照镜像。

    witness-alpha 与 witness-beta 模式和主键相同，见证行内容不同，
    用来验证“同键查哪份镜像，就只得哪份镜像的见证”。
    """
    if flavor not in {"witness-alpha", "witness-beta"}:
        raise ValueError(flavor)
    alpha = flavor == "witness-alpha"
    token = "alpha" if alpha else "beta"
    fan = 65 if alpha else 3
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE w_hub(
            code INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            partner INTEGER REFERENCES w_hub(code)
        );
        CREATE TABLE w_pair(
            tenant TEXT NOT NULL,
            seq INTEGER NOT NULL,
            title TEXT,
            next_tenant TEXT,
            next_seq INTEGER,
            PRIMARY KEY(tenant, seq),
            FOREIGN KEY(next_tenant, next_seq) REFERENCES w_pair(tenant, seq)
        ) WITHOUT ROWID;
        CREATE TABLE w_item(
            id INTEGER PRIMARY KEY,
            tenant TEXT,
            seq INTEGER,
            label TEXT,
            FOREIGN KEY(tenant, seq) REFERENCES w_pair(tenant, seq)
        );
        CREATE TABLE w_note(
            id INTEGER PRIMARY KEY,
            hub_code INTEGER REFERENCES w_hub(code),
            body TEXT
        );
        """)
        # 先以 NULL 插入，再补成环，避开语句级即时外键限制。
        db.execute("INSERT INTO w_hub(code,title,partner) VALUES(1,?,NULL)",
                    (f"{token}-hub-one",))
        db.execute("INSERT INTO w_hub(code,title,partner) VALUES(2,?,NULL)",
                    (f"{token}-hub-two",))
        db.execute("UPDATE w_hub SET partner=2 WHERE code=1")
        db.execute("UPDATE w_hub SET partner=1 WHERE code=2")
        db.execute("INSERT INTO w_pair VALUES('north',1,?,NULL,NULL)", (f"{token}-pair-one",))
        db.execute("INSERT INTO w_pair VALUES('north',2,?,NULL,NULL)", (f"{token}-pair-two",))
        db.execute("UPDATE w_pair SET next_tenant='north',next_seq=2 WHERE tenant='north' AND seq=1")
        db.execute("UPDATE w_pair SET next_tenant='north',next_seq=1 WHERE tenant='north' AND seq=2")
        db.execute("INSERT INTO w_pair VALUES('south',1,?,NULL,NULL)", (f"{token}-south",))
        db.executemany("INSERT INTO w_item(id,tenant,seq,label) VALUES(?,?,?,?)", [
            (1000 + index, "north", 1, f"{token}-child-{index:03d}") for index in range(fan)])
        db.execute("INSERT INTO w_item VALUES(2001,'north',2,?)", (f"{token}-second-key-child",))
        # 复合外键只有一列有值、另一列为 NULL：按 MATCH SIMPLE 不算引用。
        db.execute("INSERT INTO w_item VALUES(2002,'north',NULL,?)",
                    (f"{token}-null-half-key",))
        db.executemany("INSERT INTO w_note(id,hub_code,body) VALUES(?,?,?)", [
            (index, 1, f"{token}-note-{index}") for index in range(3)])
        db.commit()
        return db.serialize()
    finally:
        db.close()


def image(flavor):
    if flavor in {"witness-alpha", "witness-beta"}:
        return witness_image(flavor)
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
