"""值班演练卡的权威定义：同一模式、三份合法镜像、固定预期结局。

每张卡只有分支操作不同；镜像由 build.py 从这里生成并写入 cards/。
运行验证由 verify.py 通过真实 HTTP 入口完成。
"""

SCHEMA = """
CREATE TABLE site(
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
) WITHOUT ROWID;
CREATE TABLE rack(
    site TEXT NOT NULL REFERENCES site(code),
    slot INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('live','spare')),
    serial BLOB,
    PRIMARY KEY(site,slot)
) WITHOUT ROWID;
CREATE TABLE lease(
    id INTEGER PRIMARY KEY,
    site TEXT,
    slot INTEGER,
    holder TEXT NOT NULL,
    FOREIGN KEY(site,slot) REFERENCES rack(site,slot)
);
CREATE INDEX lease_holder ON lease(holder);
"""

BASE_ROWS = [
    ("INSERT INTO site VALUES(?,?)",
     [("north", "北区"), ("south", "南区"), ("west", "西区")]),
    ("INSERT INTO rack VALUES(?,?,?,?)",
     [("north", 1, "live", b"\x00\x14"),
      ("north", 2, "live", b"\x00\x15"),
      ("south", 1, "spare", None)]),
    ("INSERT INTO lease VALUES(?,?,?,?)",
     [(1, "north", 1, "甲"), (2, "south", 1, "乙")]),
]

# 角色 -> (文件名, 网页标签)
ROLES = {
    "base": ("quartz.sqlite", "石英祖本"),
    "left": ("cerulean.sqlite", "青蓝分支"),
    "right": ("umber.sqlite", "赤褐分支"),
}

CARDS = [
    {
        "slug": "01-unique-collision",
        "code": "D1",
        "title": "唯一键碰撞：两份合法分支在合流处撞上 UNIQUE",
        "marker": ("drill-d1", "D1 唯一键沙盘"),
        "expectation": "blocked",
        "branches": {
            "left": [
                ("UPDATE rack SET serial=? WHERE site=? AND slot=?",
                 [(b"\x00\xaa", "south", 1)]),
                ("INSERT INTO site VALUES(?,?)", [("ridge", "脊线")]),
                ("INSERT INTO rack VALUES(?,?,?,?)", [("ridge", 1, "spare", None)]),
                ("INSERT INTO lease VALUES(?,?,?,?)", [(3, "ridge", 1, "丙")]),
            ],
            "right": [
                ("INSERT INTO site VALUES(?,?)", [("mesa", "脊线")]),
                ("INSERT INTO rack VALUES(?,?,?,?)", [("mesa", 1, "spare", None)]),
                ("INSERT INTO lease VALUES(?,?,?,?)", [(4, "mesa", 1, "丁")]),
            ],
        },
        "expect_items": 7,
        "expect_unresolved": 0,
        "expect_changed": 7,
        "expect_problem": {
            "code": "constraint",
            "detail_contains": ["site", "UNIQUE"],
        },
        "lesson": [
            "左支新增 site('ridge','脊线')，右支新增 site('mesa','脊线')；两份输入各自合法。",
            "两行主键不同，计划不会把它们列为冲突，合流后却同时满足不了 site.name 的 UNIQUE。",
            "阻断发生在候选重演，不是导入；不得改输入去凑一个能通过的结果。",
        ],
    },
    {
        "slug": "02-orphan-delete",
        "code": "D2",
        "title": "删除致新增引用孤立：合法删除 × 合法新增 = 外键违规",
        "marker": ("drill-d2", "D2 孤立引用沙盘"),
        "expectation": "blocked",
        "branches": {
            "left": [
                ("DELETE FROM rack WHERE site=? AND slot=?", [("north", 2)]),
                ("UPDATE site SET name=? WHERE code=?", [("西区·左", "west")]),
            ],
            "right": [
                ("INSERT INTO lease VALUES(?,?,?,?)", [(5, "north", 2, "戊")]),
                ("UPDATE rack SET serial=? WHERE site=? AND slot=?",
                 [(b"\x00\xbb", "south", 1)]),
            ],
        },
        "expect_items": 4,
        "expect_unresolved": 0,
        "expect_changed": 4,
        "expect_problem": {
            "code": "foreign_key",
            "detail_contains": ["lease", "rack"],
        },
        "lesson": [
            "祖本有 rack('north',2)；左支删除它，右支新增引用它的 lease 5，两边在各自镜像里都合法。",
            "计划按主键把删除和新增当成两条互不相干的自动项，合流后 lease 5 的父行不存在。",
            "外键检查在候选上发现孤立引用并阻断；这不是导入错误。",
        ],
    },
    {
        "slug": "03-partial-null",
        "code": "D3",
        "title": "对照卡：复合外键部分为空不产生孤立，本来就应通过",
        "marker": ("drill-d3", "D3 部分为空沙盘"),
        "expectation": "passed",
        "branches": {
            "left": [
                ("UPDATE rack SET serial=? WHERE site=? AND slot=?",
                 [(b"\x00\xff", "north", 1)]),
                ("INSERT INTO lease VALUES(?,?,?,?)",
                 [(7, "north", None, "庚"), (8, None, 2, "辛")]),
            ],
            "right": [
                ("UPDATE site SET name=? WHERE code=?", [("西区·右", "west")]),
                ("INSERT INTO lease VALUES(?,?,?,?)",
                 [(7, "north", None, "庚"), (9, None, None, "壬")]),
            ],
        },
        "expect_items": 5,
        "expect_unresolved": 0,
        "expect_changed": 5,
        "expect_problem": None,
        "lesson": [
            "lease(site,slot) 参照 rack(site,slot)：任一列为 NULL 时 SQLite 按 MATCH SIMPLE 跳过检查。",
            "三行部分为空（7、8、9）都合法保留，候选外键检查为空——它们不是孤立。",
            "本卡不裁决、不修数据，rev 0 直接重演通过；若它被阻断，问题在环境而非数据。",
        ],
    },
]
