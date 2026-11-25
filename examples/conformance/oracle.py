"""独立判断依据（oracle）。

本模块刻意不导入 pagefold.merge / pagefold.rows / pagefold.codec，
判断依据完全独立于被测实现，来源只有两处：

1. SQLite 引擎自身的 BINARY 比较语义——通过 ``PRAGMA table_xinfo``、
   全表扫描和 ``WHERE 主键=?`` 探针取得行身份与存在性；
2. 三方合流的定义性规则（存在/缺席矩阵 + 行值相等性），在本模块内
   以原生 Python 值重新实现。

行身份归一化只复刻 SQLite 文档化的比较规则，不复刻产品代码：
- NULL 不允许出现在主键中（由目录/扫描层另行保证）；
- INTEGER 与数值精确相等的 REAL 是同一身份（``1`` 与 ``1.0`` 同键）；
- TEXT 与 BLOB 各自独立，且与任何数值都不同（``"1"``、``b"1"`` ≠ ``1``）；
- 超过 int64 范围的整值实数保留 REAL 身份，不与大整数合并；
- 复合键按位置逐分量比较，元组化后整体比较。
"""
import math
import sqlite3


def _is_int_like_real(value):
    return (isinstance(value, float) and math.isfinite(value) and value.is_integer()
            and -(2**63) <= value < 2**63)


def normalize_part(value):
    """把单个主键分量归一化为可比较的行身份分量。"""
    if value is None:
        raise ValueError("主键不允许 NULL")
    if isinstance(value, int):  # bool 是 int 子类，按数值处理
        return ("num", int(value))
    if isinstance(value, float):
        if _is_int_like_real(value):
            return ("num", int(value))
        return ("real", value)
    if isinstance(value, str):
        return ("text", value)
    if isinstance(value, bytes):
        return ("blob", value)
    raise TypeError(f"不支持的键分量类型：{type(value)!r}")


def normalize_key(values):
    return tuple(normalize_part(value) for value in values)


def open_image(data):
    db = sqlite3.connect(":memory:")
    db.deserialize(data)
    db.row_factory = sqlite3.Row
    return db


def table_info(db, name):
    fields = db.execute(f'PRAGMA main.table_xinfo("{name}")').fetchall()
    columns = [row["name"] for row in fields]
    # table_xinfo 的 pk 字段给出主键列序号 1..n（0 表示非键列）
    key = [row["name"] for row in sorted((r for r in fields if r["pk"]),
                                         key=lambda r: r["pk"])]
    return columns, key


def scan(db, name):
    """以引擎语义扫描一张表，返回 {归一化键: 原生行元组}。

    同一身份出现两次意味着产品层必然也会读到重复行；此处直接失败，
    因为构造器生成的镜像不应含重复身份。
    """
    columns, key_cols = table_info(db, name)
    positions = [columns.index(col) for col in key_cols]
    result = {}
    for raw in db.execute(f'SELECT * FROM main."{name}"'):
        row = tuple(raw)
        identity = normalize_key(row[pos] for pos in positions)
        if identity in result:
            raise AssertionError(f"{name} 出现重复行身份：{identity}")
        result[identity] = row
    return result


def engine_probe(db, name, key_cols, native_key):
    """让 SQLite 引擎自己回答「该主键身份是否存在」。

    与 Python 侧归一化互相印证：若两者结论不一致，说明 oracle 的归一化
    规则与引擎脱节，检查立即失败而不是放过。
    """
    predicate = " AND ".join(f'"{col}"=?' for col in key_cols)
    return db.execute(
        f'SELECT 1 FROM main."{name}" WHERE {predicate} LIMIT 1', native_key
    ).fetchone() is not None


class OracleSnapshot:
    def __init__(self, data):
        self.db = open_image(data)
        self.names = [row["name"] for row in db_tables(self.db)]
        self.tables = {}
        for name in self.names:
            columns, key_cols = table_info(self.db, name)
            rows = scan(self.db, name)
            # 引擎探针交叉验证每个身份
            positions = [columns.index(col) for col in key_cols]
            for identity, row in rows.items():
                native = tuple(row[pos] for pos in positions)
                if not engine_probe(self.db, name, key_cols, native):
                    raise AssertionError(f"oracle 与引擎对 {name} {native} 的存在性判断不一致")
            self.tables[name] = {"columns": columns, "key": key_cols,
                                 "positions": positions, "rows": rows}

    def close(self):
        self.db.close()


def db_tables(db):
    return db.execute(
        "SELECT name FROM main.sqlite_schema WHERE type='table' "
        "AND substr(name,1,7)<>'sqlite_' ORDER BY name"
    ).fetchall()


def classify(presence, equal_left, equal_right, branches_equal):
    """定义性三方分类。

    presence: (base, left, right) 三个布尔；equal_left/equal_right 表示
    对应支与祖本是否行值相等；branches_equal 表示左右两支彼此是否相等
    （仅在两支都存在时有意义）。返回 (automatic, 说明)，automatic ∈
    {base, left, right, None(冲突), "omit"(三支全同，不应出现在计划中)}。
    八种存在组合逐一穷举，不依赖被测代码的分支顺序。
    """
    b, l, r = presence
    if (b, l, r) == (True, True, True):
        if equal_left and equal_right:
            return "omit", "三支行值全同，计划必须省略"
        if equal_left:
            return "right", "仅右支修改"
        if equal_right:
            return "left", "仅左支修改"
        # 两支都不同于祖本：彼此同值则自动采纳，异值则冲突
        if branches_equal:
            return "left", "双支同值修改，自动采纳"
        return None, "双支异值修改，冲突"
    if (b, l, r) == (False, True, True):
        if branches_equal:
            return "left", "双支同值插入，自动采纳"
        return None, "双支异值插入，冲突"
    if (b, l, r) == (False, True, False):
        return "left", "仅左支插入"
    if (b, l, r) == (False, False, True):
        return "right", "仅右支插入"
    if (b, l, r) == (True, False, True):
        if equal_right:
            return "left", "双支同值删除，自动采纳"
        return None, "左删右改，冲突"
    if (b, l, r) == (True, True, False):
        if equal_left:
            return "right", "双支同值删除，自动采纳"
        return None, "左改右删，冲突"
    if (b, l, r) == (True, False, False):
        return "left", "双支删除，自动采纳（缺席）"
    raise AssertionError(f"不可能的存在矩阵：{presence}")


def expected_plan(base_data, left_data, right_data):
    """重算完整预期计划。

    返回列表，每项含 table、identity（oracle 归一化键）、原生键值、
    三支存在标记、三支原生行、automatic、reason。三支全同的身份不输出，
    与产品「三个分支都相同的行省略」规则对齐。
    """
    snaps = [OracleSnapshot(data) for data in (base_data, left_data, right_data)]
    try:
        items = []
        for table in snaps[0].names:
            rowsets = [snap.tables[table]["rows"] for snap in snaps]
            for identity in sorted(set().union(*rowsets), key=lambda t: repr(t)):
                brows, lrows, rrows = (rs.get(identity) for rs in rowsets)
                presence = tuple(row is not None for row in (brows, lrows, rrows))
                eq_left = lrows is not None and lrows == brows
                eq_right = rrows is not None and rrows == brows
                branches_equal = lrows is not None and rrows is not None and lrows == rrows
                automatic, reason = classify(presence, eq_left, eq_right, branches_equal)
                if automatic == "omit":
                    continue
                items.append({
                    "table": table,
                    "identity": identity,
                    "native_key": _native_key(snaps[0].tables[table], brows, lrows, rrows),
                    "presence": presence,
                    "rows": {"base": brows, "left": lrows, "right": rrows},
                    "automatic": automatic,
                    "reason": reason,
                })
        return items
    finally:
        for snap in snaps:
            snap.close()


def _native_key(table, *candidates):
    row = next(row for row in candidates if row is not None)
    return tuple(row[pos] for pos in table["positions"])


def expected_database(base_data, plan_items, decisions):
    """以祖本为起点应用预期选择，返回 {table: {identity: row}} 的完整业务值。"""
    snap = OracleSnapshot(base_data)
    try:
        result = {name: dict(snap.tables[name]["rows"]) for name in snap.names}
        for item in plan_items:
            choice = item["automatic"] or decisions[item_identity_key(item)]
            row = item["rows"][choice]
            target = result[item["table"]]
            if row is None:
                target.pop(item["identity"], None)
            else:
                target[item["identity"]] = row
        return result
    finally:
        snap.close()


def item_identity_key(item):
    return item["table"], item["identity"]


def expected_operations(plan_items, decisions):
    """预期操作痕迹：{(table, identity): ("delete"|"insert"|"update", 选择角色)}。

    产品重演固定为「先 DELETE 旧行（若祖本存在）再 INSERT 目标行（若选择
    非缺席）」，因此：
    - 祖本存在且选择缺席 → delete；
    - 祖本缺席且选择存在 → insert；
    - 两者都存在（值必不同，否则不会进计划）→ update（delete+insert 对）。
    """
    traces = {}
    for item in plan_items:
        choice = item["automatic"] or decisions[item_identity_key(item)]
        base_present = item["presence"][0]
        chosen_present = item["rows"][choice] is not None
        if base_present and not chosen_present:
            kind = "delete"
        elif not base_present and chosen_present:
            kind = "insert"
        else:
            kind = "update"
        traces[(item["table"], item["identity"])] = (kind, choice)
    return traces
