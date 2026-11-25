"""合流保证检查的合成镜像构造器。

所有输入均由声明式行规格生成，不读取任何外部文件。三份镜像共用同一套
建表语句，满足项目对「显式、非空、BINARY 比较主键」的要求；另有一份
单独构造的反例镜像用于 NULL 主键拒绝路径。
"""
import sqlite3

# 四张正向表：
# ledger  —— 存在/缺席与单改/双改矩阵，ANY 亲和，整数/实数/文本/二进制键混用
# combo   —— STRICT WITHOUT ROWID 复合键
# entry   —— 普通 rowid 表主键（隐式 rowid 变化路径）
# payload —— 带可空列的表，验证 NULL 与存储类别的保留
SCHEMA = """
CREATE TABLE ledger(
    kid  ANY    NOT NULL,
    tag  TEXT   NOT NULL,
    val  ANY,
    PRIMARY KEY(kid)
) STRICT, WITHOUT ROWID;
CREATE TABLE combo(
    a    ANY    NOT NULL,
    b    ANY    NOT NULL,
    note TEXT,
    PRIMARY KEY(a,b)
) STRICT, WITHOUT ROWID;
CREATE TABLE entry(
    code ANY    PRIMARY KEY,
    note TEXT
) STRICT;
CREATE TABLE payload(
    id   INTEGER PRIMARY KEY,
    label TEXT,
    mark ANY
) WITHOUT ROWID;
"""

# 场景行。key 用 SQLite 原生 Python 值表示（int/float/str/bytes/None）。
# branches 给出 (base, left, right) 三个角色的完整行（None 表示该支缺席）。
# expect 仅作文档与抽查用途，权威分类由 oracle 独立重算。
SCENARIOS = [
    # --- 存在 / 缺席矩阵（每行一个独立键身份） ---
    {"name": "S01_left_insert", "table": "ledger", "key": (101,),
     "branches": (None, ("L新增", 1), None), "expect": "left"},
    {"name": "S02_right_insert", "table": "ledger", "key": (102,),
     "branches": (None, None, ("R新增", 2)), "expect": "right"},
    {"name": "S03_both_insert_same", "table": "ledger", "key": (103,),
     "branches": (None, ("双插同值", 3), ("双插同值", 3)), "expect": "left"},
    {"name": "S04_both_insert_conflict", "table": "ledger", "key": (104,),
     "branches": (None, ("左值", 4), ("右值", 44)), "expect": None},
    {"name": "S05_left_delete", "table": "ledger", "key": (105,),
     "branches": (("祖本5", 5), None, ("祖本5", 5)), "expect": "left"},
    {"name": "S06_right_delete", "table": "ledger", "key": (106,),
     "branches": (("祖本6", 6), ("祖本6", 6), None), "expect": "right"},
    {"name": "S07_both_delete", "table": "ledger", "key": (107,),
     "branches": (("祖本7", 7), None, None), "expect": "left"},
    {"name": "S08_delete_vs_modify", "table": "ledger", "key": (108,),
     "branches": (("祖本8", 8), None, ("右改8", 8)), "expect": None},
    {"name": "S09_modify_vs_delete", "table": "ledger", "key": (109,),
     "branches": (("祖本9", 9), ("左改9", 9), None), "expect": None},
    # --- 单改 / 双改矩阵 ---
    {"name": "S10_left_modify", "table": "ledger", "key": (110,),
     "branches": (("祖本10", 10), ("左改10", 10), ("祖本10", 10)), "expect": "left"},
    {"name": "S11_right_modify", "table": "ledger", "key": (111,),
     "branches": (("祖本11", 11), ("祖本11", 11), ("右改11", 11)), "expect": "right"},
    {"name": "S12_both_modify_same", "table": "ledger", "key": (112,),
     "branches": (("祖本12", 12), ("同改12", 12), ("同改12", 12)), "expect": "left"},
    {"name": "S13_both_modify_conflict", "table": "ledger", "key": (113,),
     "branches": (("祖本13", 13), ("左改13", 13), ("右改13", 130)), "expect": None},
    # --- SQLite 键比较 vs 序列化表示：整数与等值实数是同一行身份 ---
    {"name": "S14_real_key_left_insert", "table": "ledger", "key": (201.0,),
     "branches": (None, ("实数键新增", 201), None), "expect": "left"},
    {"name": "S15_int_real_identity", "table": "ledger", "key": (202,),
     # 三支都以整数 202 为行身份；孪生演示在检查器内用合成快照注入实数写法
     # 202.0（SQLite 比较规则下与 202 同身份），避免被 ANY 亲和在写入时归一化。
     "branches": (("祖本202", 202), ("整数键左改动", 202), ("祖本202", 202)),
     "expect": "left"},
    {"name": "S16_real_real_identity_conflict", "table": "ledger", "key": (203.0,),
     "branches": (("祖本203", 203.0), ("左支203", 203.0), ("右支203", 2030.0)),
     "expect": None},
    {"name": "S17_text_key_distinct", "table": "ledger", "key": ("204",),
     "branches": (("文本祖本", 204), ("文本左改", 204), ("文本祖本", 204)), "expect": "left"},
    {"name": "S18_blob_key_distinct", "table": "ledger", "key": (b"205",),
     "branches": (("二进制祖本", 205), ("二进制祖本", 205), ("二进制右改", 2050)),
     "expect": "right"},
    # 9007199254740993 的整数与 double 表示在 SQLite 中不相等，必须保持两个身份
    {"name": "S19_bigint_identity", "table": "ledger", "key": (9007199254740993,),
     "branches": (("大整数祖本", 1), ("大整数左改", 1), ("大整数祖本", 1)), "expect": "left"},
    {"name": "S20_bigfloat_identity", "table": "ledger", "key": (9007199254740993.0,),
     "branches": (("大浮点祖本", 2.0), ("大浮点祖本", 2.0), ("大浮点右改", 2.0)),
     "expect": "right"},
    # -0.0 作为值（与 0.0 数值相等，但十六进制序列化不同）
    {"name": "S21_neg_zero_value", "table": "ledger", "key": (206,),
     "branches": (("零", 0.0), ("零", 0.0), ("负零", -0.0)), "expect": "right"},
    # 非键值在整数/实数/文本/二进制间切换，业务值必须保真
    {"name": "S22_value_class_switch", "table": "ledger", "key": (207,),
     "branches": (("祖本", 7), ("左文本", "7"), ("右二进制", b"\x07")), "expect": None},
    # --- 带 NULL 的列 ---
    {"name": "S23_null_preserved", "table": "payload", "key": (301,),
     "branches": ((301, "带标", None), (301, "带标", None), (301, None, "右支标")),
     "expect": "right"},
    {"name": "S24_null_flip", "table": "payload", "key": (302,),
     "branches": ((302, None, None), (302, "补值", None), (302, None, None)),
     "expect": "left"},
    {"name": "S25_null_same", "table": "payload", "key": (303,),
     "branches": ((303, None, None), (303, None, None), (303, None, None)),
     "expect": "omit"},
    # --- 复合键 ---
    {"name": "S26_composite_identity", "table": "combo", "key": ("p", 401),
     "branches": (("p", 401, "祖本"), ("p", 401.0, "左改"), ("p", 401, "祖本")),
     "expect": "left"},
    {"name": "S27_composite_single", "table": "combo", "key": ("q", 402),
     "branches": (None, None, ("q", 402, "右插")), "expect": "right"},
    {"name": "S28_composite_conflict", "table": "combo", "key": ("r", 403),
     "branches": (("r", 403, "祖本"), ("r", 403, "左"), ("r", 403.0, "右")),
     "expect": None},
    {"name": "S29_composite_delete", "table": "combo", "key": ("s", 404),
     "branches": (("s", 404, "祖本"), None, None), "expect": "left"},
    # --- rowid 表：修改行的隐式 rowid 可能变化 ---
    {"name": "S30_rowid_modify", "table": "entry", "key": ("e1",),
     "branches": (("e1", "祖本条目"), ("e1", "左改条目"), ("e1", "祖本条目")),
     "expect": "left"},
    {"name": "S31_rowid_delete_insert", "table": "entry", "key": ("e2",),
     "branches": (("e2", "祖本条目"), None, ("e2", "右支重建")), "expect": None},
]

ROLE_LABELS = ("base", "left", "right")


def _insert(db, table, key, row):
    if table == "ledger":
        db.execute("INSERT INTO ledger(kid, tag, val) VALUES(?,?,?)", (key[0], row[0], row[1]))
    elif table == "combo":
        db.execute("INSERT INTO combo(a, b, note) VALUES(?,?,?)", (row[0], row[1], row[2]))
    elif table == "entry":
        db.execute("INSERT INTO entry(code, note) VALUES(?,?)", (key[0], row[1]))
    elif table == "payload":
        db.execute("INSERT INTO payload(id, label, mark) VALUES(?,?,?)", row)
    else:  # pragma: no cover - 构造器内部表名固定
        raise ValueError(table)


def build_image(role_index):
    """按角色索引（0=祖本, 1=左支, 2=右支）生成一份内存 SQLite 镜像字节。"""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        for scenario in SCENARIOS:
            row = scenario["branches"][role_index]
            if row is not None:
                _insert(db, scenario["table"], scenario["key"], row)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def build_trio():
    """返回 (base, left, right) 三份镜像字节。"""
    return tuple(build_image(index) for index in range(3))


def build_null_key_image():
    """反例：普通 rowid 表允许 NULL 主键，应被 read_table 的 null_key 拒绝。"""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(SCHEMA)
        db.execute("CREATE TABLE nullcase(k BLOB PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO nullcase VALUES(1, 'a')")
        db.execute("INSERT INTO nullcase VALUES(NULL, 'b')")
        db.commit()
        return db.serialize()
    finally:
        db.close()


def build_twin_trio():
    """孪生演示专用三份镜像：BLOB 亲和键列保留整数/实数的原始存储类别。

    普通 ANY/INTEGER 亲和会把 202.0 在写入时归并为整数，无法在磁盘上同时
    留下两种表示；BLOB 亲和不作任何类型转换，因此可以真实构造出「存储表示
    不同、SQLite 比较相等」的同一行身份。行布局刻意让祖本持整数写法、
    左支持等值实数写法，模拟键归一化缺失时会被拆成两个身份的输入。
    """
    ddl = """
    CREATE TABLE twin(k BLOB PRIMARY KEY, tag TEXT NOT NULL, val ANY) WITHOUT ROWID;
    """
    rows = (
        (202, "祖本", 1),        # base：整数写法
        (202.0, "左支", 2),      # left：等值实数写法（BLOB 亲和保留 real）
        (202, "祖本", 1),        # right：与祖本同值
    )
    trio = []
    for row in rows:
        db = sqlite3.connect(":memory:")
        try:
            db.executescript(ddl)
            db.execute("INSERT INTO twin(k, tag, val) VALUES(?,?,?)", row)
            db.commit()
            trio.append(db.serialize())
        finally:
            db.close()
    return tuple(trio)


def build_duplicate_numeric_image():
    """反例探针：SQLite 引擎本身必须拒绝同一数值身份的整数/实数重复键。

    用于固化「1 与 1.0 是同一行身份」的引擎级证据；若未来 SQLite 语义
    改变，此构造器的断言会先失败。
    """
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE dup(k ANY PRIMARY KEY, v ANY) STRICT, WITHOUT ROWID")
        db.execute("INSERT INTO dup VALUES(1, 'int')")
        duplicate = False
        try:
            db.execute("INSERT INTO dup VALUES(1.0, 'real')")
        except sqlite3.IntegrityError:
            duplicate = True
        db.execute("CREATE TABLE probe(ok INTEGER NOT NULL)")
        db.execute("INSERT INTO probe VALUES(?)", (1 if duplicate else 0,))
        db.commit()
        return db.serialize(), duplicate
    finally:
        db.close()
