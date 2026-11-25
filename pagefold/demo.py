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


def _build(bench_labels, nodes):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO bench VALUES(?,?)", bench_labels)
        db.executemany("INSERT INTO node VALUES(?,?,?,?,?)", nodes)
        db.execute("INSERT INTO binding VALUES(1,'north',1)")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def succession_image(kind):
    """承接演练镜像。

    bad / left / right 是错误祖本计划的三方；fixed / left_fixed / right_fixed 是更正后
    承接计划的三方（分支在更正祖本时同步收敛了 west 行）。四个旧冲突键分别演示：
    north 1：旧选右支，更正后仍是冲突且右支行未变 → retained
    south 1：旧选祖本，更正后祖本行已变且仍是冲突 → needs_review
    east  1：旧选右支，更正后祖本与右支一致，系统自动采纳左支 → automatic（自动采纳与旧选择不同）
    west  1：旧选祖本，更正后三方一致，条目消失 → dropped
    """
    if kind not in {"bad", "fixed", "left", "right", "left_fixed", "right_fixed"}:
        raise ValueError(kind)
    bench = [("north", "北区"), ("south", "南区"), ("east", "东区"), ("west", "西区")]
    if kind == "bad":
        nodes = [("north", 1, "错误祖本", b"\x01", 1.0),
                 ("south", 1, "错误南区", b"\x02", 2.0),
                 ("east", 1, "错误东区", b"\x03", 3.0),
                 ("west", 1, "错误西区", b"\x04", 4.0)]
        return _build(bench, nodes)
    if kind == "fixed":
        nodes = [("north", 1, "错误祖本", b"\x01", 1.0),
                 ("south", 1, "更正南区", b"\x10", 2.5),
                 ("east", 1, "右支东区", b"\x0d", 3.3),
                 ("west", 1, "统一西区", b"\x09", 4.4)]
        return _build(bench, nodes)
    if kind == "left":
        nodes = [("north", 1, "左支北区", b"\x0a", 1.1),
                 ("south", 1, "左支南区", b"\x0a", 2.2),
                 ("east", 1, "左支东区", b"\x0a", 3.1),
                 ("west", 1, "左支旧西区", b"\x11", 4.1)]
        return _build(bench, nodes)
    if kind == "right":
        nodes = [("north", 1, "右支北区", b"\x0b", 1.2),
                 ("south", 1, "右支南区", b"\x0b", 2.3),
                 ("east", 1, "右支东区", b"\x0d", 3.3),
                 ("west", 1, "右支旧西区", b"\x12", 4.2)]
        return _build(bench, nodes)
    if kind == "left_fixed":
        nodes = [("north", 1, "左支北区", b"\x0a", 1.1),
                 ("south", 1, "左支南区", b"\x0a", 2.2),
                 ("east", 1, "左支东区", b"\x0a", 3.1),
                 ("west", 1, "统一西区", b"\x09", 4.4)]
        return _build(bench, nodes)
    # right_fixed：east 保持右支行，与祖本/左支不同，因此自动采纳左支而旧选择是右支。
    nodes = [("north", 1, "右支北区", b"\x0b", 1.2),
             ("south", 1, "右支南区", b"\x0b", 2.3),
             ("east", 1, "右支东区", b"\x0d", 3.3),
             ("west", 1, "统一西区", b"\x09", 4.4)]
    return _build(bench, nodes)