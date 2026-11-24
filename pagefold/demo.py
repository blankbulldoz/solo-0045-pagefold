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

CYCLE_SCHEMA = """
CREATE TABLE link_a(id INTEGER PRIMARY KEY, b_id INTEGER REFERENCES link_b(id));
CREATE TABLE link_b(id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES link_a(id));
"""

TANGLE_SCHEMA = """
CREATE TABLE author(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE book(id INTEGER PRIMARY KEY, author INTEGER REFERENCES author(id), title TEXT NOT NULL);
CREATE TABLE tag(code TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE book_tag(book INTEGER NOT NULL REFERENCES book(id), tag TEXT NOT NULL REFERENCES tag(code),
    PRIMARY KEY(book,tag)) WITHOUT ROWID;
CREATE TABLE note(id INTEGER PRIMARY KEY, book INTEGER REFERENCES book(id), body TEXT);
"""

SCENARIOS = ("hinge", "cycle", "tangle")


def snapshot(schema, statements):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema)
        for sql, params in statements:
            db.execute(sql, params)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def mineral(flavor):
    statements = [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        ("INSERT INTO node VALUES(?,?,?,?,?)", ("north", 1, "祖本节点", b"\x00\x14", 1.5)),
        ("INSERT INTO node VALUES(?,?,?,?,?)", ("south", 1, "稳定节点", b"", 3.0)),
        ("INSERT INTO binding VALUES(?,?,?)", (1, "north", 1)),
    ]
    if flavor == "cerulean":
        statements += [("UPDATE node SET note='左支覆写' WHERE bench='north'", ()),
                       ("INSERT INTO node VALUES(?,?,?,?,?)", ("south", 2, "左支新增", None, 4.0))]
    elif flavor == "umber":
        statements += [("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'", ()),
                       ("UPDATE bench SET label='右支更名' WHERE code='south'", ())]
    elif flavor.startswith("stress"):
        statements.append(("INSERT INTO bench VALUES(?, '共享键值')", (flavor,)))
    return snapshot(SCHEMA, statements)


def hinge(flavor):
    # 祖本有节点，左支删除节点，右支新增绑定；binding(1) 的可空复合外键始终留有一个 NULL。
    statements = [
        ("INSERT INTO bench VALUES(?,?)", ("north", "玄石")),
        ("INSERT INTO bench VALUES(?,?)", ("south", "砂层")),
        ("INSERT INTO node VALUES(?,?,?,?,?)", ("north", 1, "铰链节点", b"\x00\x14", 1.5)),
        ("INSERT INTO node VALUES(?,?,?,?,?)", ("south", 1, "稳定节点", b"", 3.0)),
        ("INSERT INTO binding VALUES(?,?,?)", (1, "north", None)),
    ]
    if flavor == "hinge-left":
        statements += [("UPDATE binding SET bench='south' WHERE id=1", ()),
                       ("DELETE FROM node WHERE bench='north' AND slot=1", ())]
    elif flavor == "hinge-right":
        statements.append(("INSERT INTO binding VALUES(?,?,?)", (2, "north", 1)))
    return snapshot(SCHEMA, statements)


def cycle(flavor):
    # 左支让 link_a 指向 link_b，右支让 link_b 指向 link_a，循环依赖双方更新后才成立。
    statements = [
        ("INSERT INTO link_a VALUES(?,?)", (1, None)),
        ("INSERT INTO link_a VALUES(?,?)", (2, None)),
        ("INSERT INTO link_b VALUES(?,?)", (1, None)),
        ("INSERT INTO link_b VALUES(?,?)", (2, None)),
    ]
    if flavor == "cycle-left":
        statements.append(("UPDATE link_a SET b_id=1 WHERE id=1", ()))
    elif flavor == "cycle-right":
        statements.append(("UPDATE link_b SET a_id=1 WHERE id=1", ()))
    return snapshot(CYCLE_SCHEMA, statements)


def tangle(flavor):
    # 大量无关的外键关系之间藏着两个联动组和一个待裁决组。
    statements = [
        ("INSERT INTO author VALUES(?,?)", (1, "岑")),
        ("INSERT INTO author VALUES(?,?)", (2, "苇")),
        ("INSERT INTO author VALUES(?,?)", (3, "岚")),
        ("INSERT INTO author VALUES(?,?)", (4, "淼")),
        ("INSERT INTO book VALUES(?,?,?)", (1, 1, "航标")),
        ("INSERT INTO book VALUES(?,?,?)", (2, 1, "旧题")),
        ("INSERT INTO book VALUES(?,?,?)", (3, 2, "潮间带")),
        ("INSERT INTO book VALUES(?,?,?)", (4, 2, "砾层")),
        ("INSERT INTO book VALUES(?,?,?)", (5, 3, "雾线")),
        ("INSERT INTO book VALUES(?,?,?)", (6, 1, "赤滩")),
        ("INSERT INTO tag VALUES(?,?)", ("a", "潮位")),
        ("INSERT INTO tag VALUES(?,?)", ("b", "风向")),
        ("INSERT INTO tag VALUES(?,?)", ("c", "盐度")),
        ("INSERT INTO book_tag VALUES(?,?)", (1, "a")),
        ("INSERT INTO book_tag VALUES(?,?)", (2, "b")),
        ("INSERT INTO book_tag VALUES(?,?)", (3, "a")),
        ("INSERT INTO book_tag VALUES(?,?)", (3, "c")),
        ("INSERT INTO book_tag VALUES(?,?)", (5, "b")),
        ("INSERT INTO book_tag VALUES(?,?)", (6, "c")),
        ("INSERT INTO note VALUES(?,?,?)", (1, 1, "祖本备注一")),
        ("INSERT INTO note VALUES(?,?,?)", (2, 3, "祖本备注二")),
        ("INSERT INTO note VALUES(?,?,?)", (3, 5, "祖本备注三")),
        ("INSERT INTO note VALUES(?,?,?)", (4, 2, "待删备注")),
        ("INSERT INTO note VALUES(?,?,?)", (5, 3, "祖本备注五")),
        ("INSERT INTO note VALUES(?,?,?)", (6, 6, "祖本备注六")),
    ]
    if flavor == "tangle-left":
        statements += [
            ("INSERT INTO note VALUES(?,?,?)", (9, 1, "左支备注")),
            ("UPDATE book SET title='左支修订' WHERE id=2", ()),
            ("UPDATE note SET body='左支备注二改' WHERE id=2", ()),
            ("DELETE FROM note WHERE id=4", ()),
            ("DELETE FROM author WHERE id=4", ()),
        ]
    elif flavor == "tangle-right":
        statements += [
            ("INSERT INTO tag VALUES(?,?)", ("d", "右支标签")),
            ("UPDATE author SET name='右支更名' WHERE id=3", ()),
            ("UPDATE book SET title='右支题名' WHERE id=3", ()),
            ("UPDATE book SET author=4 WHERE id=6", ()),
            ("UPDATE note SET body='右支备注二改' WHERE id=2", ()),
        ]
    return snapshot(TANGLE_SCHEMA, statements)


def image(flavor):
    known = {"quartz", "cerulean", "umber", "stress-left", "stress-right"}
    known |= {f"{scenario}-{role}" for scenario in SCENARIOS for role in ("base", "left", "right")}
    if flavor not in known:
        raise ValueError(flavor)
    if flavor.startswith("hinge-"):
        return hinge(flavor)
    if flavor.startswith("cycle-"):
        return cycle(flavor)
    if flavor.startswith("tangle-"):
        return tangle(flavor)
    return mineral(flavor)
