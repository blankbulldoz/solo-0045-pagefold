"""四组评审样本的共享定义：模式、数据、变更、裁决策略与预期结果。

make_samples 与 run_review 共用本模块，保证样本、文件清单与观察记录三者一致。
所有数据均为确定性的（不含随机因素），重复生成得到逐字节相同的镜像。
"""

SCHEMA = """
CREATE TABLE entry(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    qty INTEGER NOT NULL,
    weight REAL,
    note TEXT
) STRICT;
CREATE TABLE tag(
    entry_id INTEGER NOT NULL REFERENCES entry(id),
    label TEXT NOT NULL,
    PRIMARY KEY(entry_id,label)
) STRICT, WITHOUT ROWID;
CREATE INDEX tag_label ON tag(label);
"""

# 各表主键列（独立差异对比与 run_review 的裁决定位共用）
KEY_COLUMNS = {"entry": ("id",), "tag": ("entry_id", "label")}


def fill(db, count, note_of):
    """写入 entry 1..count 及对应 't1' 标签；qty = id % 50，weight = id * 0.5。"""
    db.execute("BEGIN")
    db.executemany(
        "INSERT INTO entry VALUES(?,?,?,?,?)",
        [(i, f"item-{i:05d}", i % 50, i * 0.5, note_of(i)) for i in range(1, count + 1)])
    db.executemany("INSERT INTO tag VALUES(?,?)", [(i, "t1") for i in range(1, count + 1)])
    db.execute("COMMIT")


def base_a(db):
    fill(db, 8, lambda i: f"祖本记录-{i}")
    db.executemany("INSERT INTO tag VALUES(?,?)", [(2, "t2"), (3, "t2")])


def base_b(db):
    fill(db, 2000, lambda i: None)


def base_c(db):
    fill(db, 8000, lambda i: None)


def base_d(db):
    fill(db, 24, lambda i: f"祖本-{i}")


A_LEFT = [
    ("UPDATE entry SET note='左支改写-1' WHERE id=1", ()),
    ("INSERT INTO entry VALUES(9,'item-00009',59,4.5,'左支新增-9')", ()),
    ("INSERT INTO tag VALUES(9,'t1')", ()),
]
A_RIGHT = [
    ("UPDATE entry SET qty=999 WHERE id=1", ()),
    ("UPDATE entry SET note='右支改写-3' WHERE id=3", ()),
    ("DELETE FROM tag WHERE entry_id=2 AND label='t2'", ()),
]

B_LEFT = [
    ("UPDATE entry SET qty=qty+7, note='左支密集更新' WHERE id%2=0", ()),
    ("INSERT INTO entry VALUES(?,?,?,?,?)",
     [(i, f"item-{i:05d}", i % 50, i * 0.5, "左支新增") for i in range(2001, 2201)]),
    ("INSERT INTO tag VALUES(?,?)", [(i, "t1") for i in range(2001, 2201)]),
]
B_RIGHT = [
    ("UPDATE entry SET weight=weight+0.25 WHERE id%20=1", ()),
]

C_LEFT = [
    ("UPDATE entry SET note='左支微调-100' WHERE id=100", ()),
    ("UPDATE entry SET note='左支冲突-200' WHERE id=200", ()),
    ("INSERT INTO entry VALUES(8001,'item-08001',1,4000.5,'左支新增-8001')", ()),
    ("INSERT INTO tag VALUES(8001,'t1')", ()),
]
C_RIGHT = [
    ("UPDATE entry SET note='右支冲突-200' WHERE id=200", ()),
    ("UPDATE entry SET qty=qty+5 WHERE id=300", ()),
    ("DELETE FROM tag WHERE entry_id=7000 AND label='t1'", ()),
    ("DELETE FROM entry WHERE id=7000", ()),
]

D_LEFT = [("UPDATE entry SET note=? WHERE id=?", (f"左支-{i}", i)) for i in range(1, 7)] + [
    ("INSERT INTO entry VALUES(100,'item-00100',0,50.0,'左支新增-100')", ()),
    ("INSERT INTO tag VALUES(100,'t1')", ()),
]
D_RIGHT = [("UPDATE entry SET qty=? WHERE id=?", (1000 + i, i)) for i in range(1, 7)] + [
    ("DELETE FROM tag WHERE entry_id=24 AND label='t1'", ()),
    ("DELETE FROM entry WHERE id=24", ()),
]

GROUPS = [
    {
        "id": "a-small-conflict",
        "title": "小冲突",
        "purpose": "少量行分歧：核对逐行裁决、版本拦截、封存与归档流程。",
        "build_base": base_a,
        "base_note": "entry 1–8（qty=id%50，note=祖本记录-i）；每个 entry 一条 tag 't1'，"
                     "entry 2、3 各多一条 't2'（共 8 + 10 行）。",
        "left": {"sql": A_LEFT,
                 "note": "改写 entry 1 的 note；新增 entry 9 及其 't1' 标签。"},
        "right": {"sql": A_RIGHT,
                  "note": "改写 entry 1 的 qty；改写 entry 3 的 note；删除 tag (2,'t2')。"},
        "decision_note": "entry 1 的冲突选右支（qty=999，note 保持祖本值）。",
        "decisions": {("entry", (1,)): "right"},
        "expect": {"items": 5, "automatic": 4, "conflicts": 1, "changed": 5,
                   "rows": {"entry": 9, "tag": 10}},
        "content_checks": [
            ("SELECT qty FROM entry WHERE id=1", 999),
            ("SELECT note FROM entry WHERE id=1", "祖本记录-1"),
            ("SELECT note FROM entry WHERE id=3", "右支改写-3"),
            ("SELECT COUNT(*) FROM tag WHERE entry_id=2 AND label='t2'", 0),
            ("SELECT note FROM entry WHERE id=9", "左支新增-9"),
        ],
        "archive": True,
    },
    {
        "id": "b-dense-auto",
        "title": "密集自动更新",
        "purpose": "单侧高密度更新：计划条目多，但全部自动采纳、零冲突。",
        "build_base": base_b,
        "base_note": "entry 1–2000（note 为 NULL）；每个 entry 一条 tag 't1'（共 4,000 行）。",
        "left": {"sql": B_LEFT,
                 "note": "改写全部偶数 id（1,000 行）的 qty 与 note；"
                         "新增 entry 2001–2200 及其标签（400 行）。"},
        "right": {"sql": B_RIGHT,
                  "note": "改写 id%20=1 的 100 行 weight（与左支改动不重叠）。"},
        "decision_note": "无冲突，不裁决，直接重演。",
        "decisions": {},
        "expect": {"items": 1500, "automatic": 1500, "conflicts": 0, "changed": 1500,
                   "rows": {"entry": 2200, "tag": 2200}},
        "content_checks": [
            ("SELECT COUNT(*) FROM entry WHERE note='左支密集更新'", 1000),
            ("SELECT qty FROM entry WHERE id=2", 9),
            ("SELECT weight FROM entry WHERE id=1", 0.75),
            ("SELECT COUNT(*) FROM entry WHERE id BETWEEN 2001 AND 2200", 200),
        ],
    },
    {
        "id": "c-bulk-identical",
        "title": "大量相同行",
        "purpose": "三份镜像 99.96% 行相同：验证大量相同键不会被误计为大量变更。",
        "build_base": base_c,
        "base_note": "entry 1–8000（note 为 NULL）；每个 entry 一条 tag 't1'"
                     "（共 16,000 行，接近 20,000 行上限）。",
        "left": {"sql": C_LEFT,
                 "note": "改写 entry 100、200 的 note；新增 entry 8001 及其标签。"},
        "right": {"sql": C_RIGHT,
                  "note": "改写 entry 200（与左支不同值）与 entry 300；删除 entry 7000 及其标签。"},
        "decision_note": "entry 200 的冲突选左支（note=左支冲突-200）。",
        "decisions": {("entry", (200,)): "left"},
        "expect": {"items": 7, "automatic": 6, "conflicts": 1, "changed": 7,
                   "rows": {"entry": 8000, "tag": 8000}},
        "content_checks": [
            ("SELECT note FROM entry WHERE id=200", "左支冲突-200"),
            ("SELECT note FROM entry WHERE id=100", "左支微调-100"),
            ("SELECT qty FROM entry WHERE id=300", 5),
            ("SELECT COUNT(*) FROM entry WHERE id=7000", 0),
            ("SELECT COUNT(*) FROM entry WHERE id=8001", 1),
        ],
    },
    {
        "id": "d-noop-decisions",
        "title": "空转裁决（有效成果刻意少于计划项）",
        "purpose": "6 项冲突中 4 项刻意裁决为祖本：计划 10 条，实际变更只有 6 行。",
        "build_base": base_d,
        "base_note": "entry 1–24（note=祖本-i）；每个 entry 一条 tag 't1'（共 48 行）。",
        "left": {"sql": D_LEFT,
                 "note": "改写 entry 1–6 的 note；新增 entry 100 及其标签。"},
        "right": {"sql": D_RIGHT,
                  "note": "改写 entry 1–6 的 qty（与左支冲突）；删除 entry 24 及其标签。"},
        "decision_note": "entry 1–4 选祖本（空转，不产生变更），entry 5 选左支，entry 6 选右支。",
        "decisions": {**{("entry", (i,)): "base" for i in range(1, 5)},
                      ("entry", (5,)): "left", ("entry", (6,)): "right"},
        "expect": {"items": 10, "automatic": 4, "conflicts": 6, "changed": 6,
                   "rows": {"entry": 24, "tag": 24}},
        "content_checks": [
            ("SELECT note FROM entry WHERE id=1", "祖本-1"),
            ("SELECT qty FROM entry WHERE id=1", 1),
            ("SELECT note FROM entry WHERE id=4", "祖本-4"),
            ("SELECT note FROM entry WHERE id=5", "左支-5"),
            ("SELECT qty FROM entry WHERE id=6", 1006),
            ("SELECT note FROM entry WHERE id=6", "祖本-6"),
            ("SELECT COUNT(*) FROM entry WHERE id=24", 0),
            ("SELECT COUNT(*) FROM entry WHERE id=100", 1),
        ],
    },
]
