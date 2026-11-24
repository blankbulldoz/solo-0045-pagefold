"""两组三方镜像：展示结构差异的精确分类诊断。

common 组：左支与右支各自多出一个不同的单独索引（共同差异类别，各自差异实例）。
branch 组：左支只交换 ledger 复合主键顺序，右支只新增一个单独索引，
          两者都不得被笼统报成“缺表”。
所有镜像均满足目录约束（显式非空 BINARY 主键、无视图/触发器/AUTOINCREMENT）。
"""
import sqlite3

BASE_SCHEMA = """
CREATE TABLE owner(code TEXT PRIMARY KEY, name TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE ledger(
    book TEXT NOT NULL REFERENCES owner(code), seq INTEGER NOT NULL, note TEXT NOT NULL,
    PRIMARY KEY(book,seq)
) WITHOUT ROWID;
CREATE TABLE entry(
    id INTEGER PRIMARY KEY, book TEXT NOT NULL, tag TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0)
);
CREATE INDEX entry_book ON entry(book);
"""

ROWS = [
    ("INSERT INTO owner VALUES(?,?)", [("alpha", "甲账套"), ("beta", "乙账套")]),
    ("INSERT INTO ledger VALUES(?,?,?)", [("alpha", 1, "期初"), ("alpha", 2, "结转"), ("beta", 1, "盘点")]),
    ("INSERT INTO entry VALUES(?,?,?,?)", [(1, "alpha", "in", 12.5), (2, "alpha", "out", 3.0),
                                           (3, "beta", "in", 7.25)]),
]


def build(schema):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema)
        for statement, values in ROWS:
            db.executemany(statement, values)
        db.commit()
        return db.serialize()
    finally:
        db.close()


SCHEMAS = {
    "common-base": BASE_SCHEMA,
    "common-left": BASE_SCHEMA + "CREATE UNIQUE INDEX ledger_note ON ledger(note);\n",
    "common-right": BASE_SCHEMA + "CREATE INDEX entry_tag ON entry(tag);\n",
    "branch-base": BASE_SCHEMA,
    "branch-left": """
CREATE TABLE owner(code TEXT PRIMARY KEY, name TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE ledger(
    book TEXT NOT NULL REFERENCES owner(code), seq INTEGER NOT NULL, note TEXT NOT NULL,
    PRIMARY KEY(seq,book)
) WITHOUT ROWID;
CREATE TABLE entry(
    id INTEGER PRIMARY KEY, book TEXT NOT NULL, tag TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0)
);
CREATE INDEX entry_book ON entry(book);
""",
    "branch-right": BASE_SCHEMA + "CREATE INDEX ledger_book_note ON ledger(book,note);\n",
}

GROUPS = {
    "common": ("common-base", "common-left", "common-right"),
    "branch": ("branch-base", "branch-left", "branch-right"),
}


def case_image(name):
    if name not in SCHEMAS:
        raise ValueError(name)
    return build(SCHEMAS[name])
