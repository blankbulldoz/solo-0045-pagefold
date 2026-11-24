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

PORT_FLAVORS = ("port-base", "port-branch", "port-target", "port-blocked-target")


def image(flavor):
    if flavor not in {"quartz", "cerulean", "umber", "stress-left", "stress-right",
                      "port-base", "port-branch", "port-target", "port-blocked-target"}:
        raise ValueError(flavor)
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO bench VALUES(?,?)", [("north", "玄石"), ("south", "砂层")])
        db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
            ("north", 1, "祖本节点", b"\x00\x14", 1.5), ("south", 1, "稳定节点", b"", 3.0)])
        db.execute("INSERT INTO binding VALUES(1,'north',1)")
        if flavor in PORT_FLAVORS:
            # 移植素材引入 west/east 两个工作台，先补齐外键父行
            db.executemany("INSERT INTO bench VALUES(?,?)",
                           [("west", "西台"), ("east", "东台")])
        if flavor == "cerulean":
            db.execute("UPDATE node SET note='左支覆写' WHERE bench='north'")
            db.execute("INSERT INTO node VALUES('south',2,'左支新增',NULL,4.0)")
        elif flavor == "umber":
            db.execute("UPDATE node SET payload=x'ff00',weight=2.25 WHERE bench='north'")
            db.execute("UPDATE bench SET label='右支更名' WHERE code='south'")
        elif flavor.startswith("stress"):
            db.execute("INSERT INTO bench VALUES(?, '共享键值')", (flavor,))
        elif flavor == "port-base":
            # 上轮祖本：改名前的节点 ('west',1) 与引用它的 binding(2)
            db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
                ("west", 1, "待改名节点", b"\x01\x02", 2.5),
                ("east", 1, "待更新节点", b"", 5.0)])
            db.execute("INSERT INTO binding VALUES(2,'west',1)")
        elif flavor == "port-branch":
            # 改动分支：复合主键改名 (west,1)->(west,2)、更新 (east,1)、新增 (south,3)，
            # 并把引用 binding(2) 重新指向新键。
            db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
                ("west", 2, "待改名节点", b"\x01\x02", 2.5),
                ("east", 1, "分支更新值", b"", 6.5),
                ("south", 3, "分支新增节点", None, 7.0)])
            db.execute("INSERT INTO binding VALUES(2,'west',2)")
        elif flavor == "port-target":
            # 新祖本：本轮业务已提前落下部分分支改动，但把另一些行改成别的值。
            # 改名的新键 (west,2) 已到位；旧键 (west,1) 仍保留并仍被 binding(2) 引用。
            db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
                ("west", 1, "本轮保留旧键", b"\x01\x02", 2.5),
                ("west", 2, "待改名节点", b"\x01\x02", 2.5),
                ("east", 1, "本轮业务改值", b"", 9.0),
                ("south", 3, "分支新增节点", None, 7.0)])
            db.execute("INSERT INTO binding VALUES(2,'west',1)")
        elif flavor == "port-blocked-target":
            # 与 port-target 相同，但多一个仍指向旧键 (west,1) 的引用，
            # 使"重新选择完整改名"在删除旧键时被外键拦截。
            db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", [
                ("west", 1, "本轮保留旧键", b"\x01\x02", 2.5),
                ("west", 2, "待改名节点", b"\x01\x02", 2.5),
                ("east", 1, "本轮业务改值", b"", 9.0),
                ("south", 3, "分支新增节点", None, 7.0)])
            db.executemany("INSERT INTO binding VALUES(?, ?, ?)",
                           [(2, "west", 1), (3, "west", 1)])
        db.commit()
        return db.serialize()
    finally:
        db.close()
