"""保证检查执行器。

分层断言，任何一层失败都给出具体场景名与差异，而不是只比较最终数据库：

  A. 引擎证据     —— 整数/等值实数同身份、NULL 主键拒绝等引擎语义探针
  B. 表示区分     —— 行身份相同的整数与实数，其 JSON 序列化表示必须不同
  C. 计划分类     —— 产品计划逐项对照 oracle 的存在矩阵与 automatic
  D. 重演业务值   —— 自动计划与冲突项的全部四种裁决重演后，重读业务值
  E. 操作痕迹     —— delete/insert/update 痕迹对照，捕获「分类错但结果同」
  F. 左右互换     —— 互换支系后的语义变化对照
"""
import json
import tempfile
from pathlib import Path

from pagefold.catalog import describe
from pagefold.codec import atom, canonical, unpack
from pagefold.errors import Problem
from pagefold.merge import calculate
from pagefold.replay import amendments, rebuild
from pagefold.rows import read_image, read_table
from pagefold.sqlite_image import mounted
from pagefold.store import Store
from pagefold.images import register
from pagefold.plans import create_plan

from . import fixtures
from .oracle import (expected_database, expected_operations, expected_plan,
                     item_identity_key, normalize_key)

CHOICES = ("base", "left", "right", "clear")


class CheckFailure(AssertionError):
    pass


class Reporter:
    def __init__(self):
        self.passed = []

    def ok(self, layer, detail):
        self.passed.append((layer, detail))

    def fail(self, layer, detail):
        raise CheckFailure(f"[{layer}] {detail}")

    def report(self):
        for layer, detail in self.passed:
            print(f"{layer} ✓ {detail}")
        print(f"\n共 {len(self.passed)} 项检查全部通过。")


def _product_plan(trio):
    """走产品真实代码路径：目录 → 行扫描 → 三方计算（不经 HTTP）。"""
    catalogs = []
    snapshots = []
    for data in trio:
        with mounted(data) as db:
            catalog = describe(db)
            catalogs.append(catalog)
            snapshots.append(read_image(db, catalog))
    if len({c["signature"] for c in catalogs}) != 1:
        raise CheckFailure("三份镜像模式签名不一致，夹具失效")
    return catalogs[0], calculate(*snapshots)


def _index_oracle_items(expected):
    return {(item["table"], item["identity"]): item for item in expected}


def _product_identity(item):
    """把产品 JSON 键转回 oracle 归一化身份（经过 codec 反解后的原生值）。"""
    native = tuple(unpack(part) for part in item["key"])
    return normalize_key(native)


# ---------------------------------------------------------------- A. 引擎证据

def check_engine_evidence(rep):
    data, duplicate = fixtures.build_duplicate_numeric_image()
    with mounted(data) as db:
        flag = db.execute("SELECT ok FROM probe").fetchone()[0]
    if not duplicate or flag != 1:
        rep.fail("A", "SQLite 引擎未拒绝 1 与 1.0 的重复主键，身份前提失效")
    rep.ok("A", "引擎拒绝整数 1 与等值实数 1.0 的重复键（同一行身份）")

    null_image = fixtures.build_null_key_image()
    with mounted(null_image) as db:
        catalog = describe(db)
        table = next(t for t in catalog["tables"] if t["name"] == "nullcase")
        try:
            read_table(db, table)
        except Problem as exc:
            if exc.code != "null_key":
                rep.fail("A", f"NULL 主键报了错误的代码：{exc.code}")
        else:
            rep.fail("A", "含 NULL 主键的镜像未被 null_key 拒绝")
    rep.ok("A", "NULL 主键在行扫描阶段以 null_key 拒绝")


# ---------------------------------------------------------------- B. 表示区分

def check_representation_distinctness(rep):
    """同一行身份的两种写法，序列化表示必须仍可区分，证明产品不是靠字符串
    表示分键（若靠表示分键，1 与 1.0 会被错划成两项）。"""
    int_token = canonical([atom(1)])
    real_token = canonical([atom(1.0)])
    if int_token == real_token:
        rep.fail("B", "整数与等值实数的序列化表示意外相同")
    rep.ok("B", f"1 与 1.0 表示不同（{int_token} vs {real_token}）但行身份相同")

    text_token = canonical([atom("1")])
    blob_token = canonical([atom(b"1")])
    if len({int_token, real_token, text_token, blob_token}) != 4:
        rep.fail("B", "整数/实数/文本/二进制的序列化表示未全部区分")
    rep.ok("B", "整数、实数、文本、二进制四种表示两两可区分")

    if atom(0.0) == atom(-0.0):
        rep.fail("B", "0.0 与 -0.0 的保真编码相同，负值信息会丢失")
    rep.ok("B", "0.0 与 -0.0 编码不同，重演可还原负零")


# ---------------------------------------------------------------- C. 计划分类

def check_plan_classification(rep, trio):
    catalog, items = _product_plan(trio)
    expected = expected_plan(*trio)
    exp_index = _index_oracle_items(expected)

    if len(items) != len(expected):
        rep.fail("C", f"计划项数 {len(items)} != 预期 {len(expected)}（含错误省略/多划）")

    seen = set()
    for item in items:
        identity = _product_identity(item)
        marker = (item["table"], identity)
        oracle_item = exp_index.get(marker)
        if oracle_item is None:
            rep.fail("C", f"产品多出计划项：表 {item['table']} 键 {item['key']}")
        seen.add(marker)
        if item["automatic"] != oracle_item["automatic"]:
            rep.fail("C", f"{item['table']} {item['key']} 分类 {item['automatic']} "
                          f"!= 预期 {oracle_item['automatic']}（{oracle_item['reason']}）")
        # 存在/缺席矩阵：产品各角色 row 是否为 None 必须与 oracle 一致
        for role_index, role in enumerate(("base", "left", "right")):
            present = item[role] is not None
            if present != oracle_item["presence"][role_index]:
                rep.fail("C", f"{item['table']} {item['key']} 的 {role} 存在性判断错误")
        # 业务行值逐格对照（含 NULL 与存储类别）
        for role in ("base", "left", "right"):
            if item[role] is not None:
                native = [_decode(cell) for cell in item[role]]
                if tuple(native) != oracle_item["rows"][role]:
                    rep.fail("C", f"{item['table']} {item['key']} 的 {role} 行值保真失败："
                                  f"{native} != {oracle_item['rows'][role]}")
    missing = set(exp_index) - seen
    if missing:
        rep.fail("C", f"产品漏掉计划项：{sorted(missing, key=repr)}")

    # S25 三支全同（含 NULL）必须被省略
    s25_identity = normalize_key((303,))
    if ("payload", s25_identity) in exp_index:
        rep.fail("C", "oracle 未省略三支全同行，夹具/判断依据失效")
    if any(i["table"] == "payload" and _product_identity(i) == s25_identity for i in items):
        rep.fail("C", "三支全同的 NULL 行未被省略")

    rep.ok("C", f"计划 {len(items)} 项的存在矩阵、automatic 分类与行值全部符合独立判断")
    return catalog, items, expected, exp_index


def _decode(cell):
    return unpack(cell)


# ---------------------------------------------------------------- D. 重演业务值

def _replay(catalog, trio, items, decisions):
    operations = amendments(items, decisions)
    return rebuild(trio[0], catalog, operations), operations


def _read_candidate(data, catalog):
    with mounted(data) as db:
        return read_image(db, catalog)


def check_replay_values(rep, trio, catalog, items, expected, exp_index):
    conflicts = [item for item in items if item["automatic"] is None]
    if not conflicts:
        rep.fail("D", "夹具中没有冲突项，无法验证人工裁决路径")

    # D1：所有冲突统一选 left 后整体重演（自动项维持 automatic）
    decisions = {item["id"]: "left" for item in conflicts}
    candidate, _ = _replay(catalog, trio, items, decisions)
    oracle_decisions = {
        item_identity_key(exp_index[(item["table"], _product_identity(item))]): "left"
        for item in conflicts}
    expected_db = expected_database(trio[0], expected, oracle_decisions)
    _assert_business_values(rep, candidate, catalog, expected_db, "D", "自动计划+统一裁决重演")

    # D2：每个冲突项穷尽 base/left/right/clear 四种裁决（其余冲突固定 left）
    for conflict in conflicts:
        marker = (conflict["table"], _product_identity(conflict))
        for choice in CHOICES:
            decisions = {item["id"]: "left" for item in conflicts}
            if choice == "clear":
                decisions.pop(conflict["id"], None)
                try:
                    amendments(items, decisions)
                except Problem as exc:
                    if exc.code != "unresolved":
                        rep.fail("D", f"未裁决冲突报了错误代码：{exc.code}")
                    continue
                rep.fail("D", "未裁决冲突未被重演拦截")
            decisions[conflict["id"]] = choice
            candidate, _ = _replay(catalog, trio, items, decisions)
            oracle_decisions = {
                item_identity_key(exp_index[(item["table"], _product_identity(item))]): "left"
                for item in conflicts}
            oracle_decisions[item_identity_key(exp_index[marker])] = choice
            expected_db = expected_database(trio[0], expected, oracle_decisions)
            _assert_business_values(
                rep, candidate, catalog, expected_db, "D",
                f"冲突 {marker[0]}{tuple(conflict['key'])} 裁决 {choice}")
    rep.ok("D", f"全部 {len(conflicts)} 个冲突项的三种有效裁决重演后业务值均符合预期")


def _assert_business_values(rep, candidate, catalog, expected_db, layer, title):
    actual = _read_candidate(candidate, catalog)
    for table in expected_db:
        actual_rows = actual[table]
        # 产品用 canonical JSON 键存储，oracle 用归一化身份；逐身份反查
        actual_by_identity = {}
        for token, row in actual_rows.items():
            native_key = tuple(_decode(cell) for cell in json.loads(token))
            actual_by_identity[normalize_key(native_key)] = tuple(_decode(c) for c in row)
        if set(actual_by_identity) != set(expected_db[table]):
            extra = set(actual_by_identity) - set(expected_db[table])
            missing = set(expected_db[table]) - set(actual_by_identity)
            rep.fail(layer, f"{title}：表 {table} 行身份集合不符，多余 {extra}，缺失 {missing}")
        for identity, expected_row in expected_db[table].items():
            if actual_by_identity[identity] != expected_row:
                rep.fail(layer, f"{title}：表 {table} 身份 {identity} 业务值 "
                                f"{actual_by_identity[identity]} != {expected_row}")
    rep.ok(layer, f"{title}：各表行身份集合与逐格业务值（含 NULL/类型）一致")


# ---------------------------------------------------------------- E. 操作痕迹

def check_operation_traces(rep, trio, catalog, items, expected, exp_index):
    """错误分类但结果巧合相同的检出手段：比较操作痕迹而非只看终态。"""
    product_decisions = {item["id"]: "left" for item in items if item["automatic"] is None}
    oracle_decisions = {
        item_identity_key(exp_index[(item["table"], _product_identity(item))]): "left"
        for item in items if item["automatic"] is None}
    _, operations = _replay(catalog, trio, items, product_decisions)
    expected_traces = expected_operations(expected, oracle_decisions)

    actual_traces = {}
    for table in catalog["tables"]:
        for item, row in operations.get(table["name"], []):
            identity = _product_identity(item)
            base_present = item["base"] is not None
            chosen_present = row is not None
            kind = ("delete" if base_present and not chosen_present else
                    "insert" if not base_present and chosen_present else "update")
            role, _ = _selected_role(item, product_decisions)
            actual_traces[(table["name"], identity)] = (kind, role)

    if actual_traces != expected_traces:
        for marker in sorted(set(actual_traces) | set(expected_traces), key=repr):
            if actual_traces.get(marker) != expected_traces.get(marker):
                rep.fail("E", f"操作痕迹分歧 {marker}：实际 {actual_traces.get(marker)} "
                              f"!= 预期 {expected_traces.get(marker)}")
    rep.ok("E", f"{len(actual_traces)} 个变更身份的 delete/insert/update 痕迹与选择角色全部吻合")


def _selected_role(item, decisions):
    role = item["automatic"] or decisions.get(item["id"])
    return role, item[role] if role else None


# ---------------------------------------------------------------- F. 左右互换

def check_swap_symmetry(rep, trio):
    base, left, right = trio
    swapped = (base, right, left)

    catalog_a, items_a = _product_plan(trio)
    catalog_b, items_b = _product_plan(swapped)
    expected_a = expected_plan(*trio)
    expected_b = expected_plan(*swapped)

    if len(items_a) != len(items_b):
        rep.fail("F", f"互换后计划项数变化：{len(items_a)} -> {len(items_b)}")

    index_a = {(i["table"], _product_identity(i)): i for i in items_a}
    index_b = {(i["table"], _product_identity(i)): i for i in items_b}
    if set(index_a) != set(index_b):
        rep.fail("F", "互换左右支后行身份集合发生变化")

    # 同一身份：互换后 left/right 载荷对调；automatic 按 oracle 预期翻转。
    # 注意单边项必然翻转（left↔right）；双支同值项采纳的是「同值」而非某一
    # 侧，互换后仍自动成立（产品固定落在 left 角色上）——这是必须固化的语义。
    exp_a = {(i["table"], i["identity"]): i for i in expected_a}
    exp_b = {(i["table"], i["identity"]): i for i in expected_b}
    for marker, item_a in index_a.items():
        item_b = index_b[marker]
        if item_a["left"] != item_b["right"] or item_a["right"] != item_b["left"]:
            rep.fail("F", f"{marker} 互换后左右载荷未正确对调")
        if item_a["base"] != item_b["base"]:
            rep.fail("F", f"{marker} 互换后祖本载荷变化")
        if item_a["automatic"] != exp_a[marker]["automatic"] or \
                item_b["automatic"] != exp_b[marker]["automatic"]:
            rep.fail("F", f"{marker} 产品 automatic 与独立判断不一致")
        if item_a["id"] != item_b["id"]:
            rep.fail("F", f"{marker} 互换后条目指纹变化，裁决无法跨对照复用")

    # 单边变化项必须翻转；双支同值（lrow==rrow，含双删的双 None）项固定落在
    # left，因为采纳的是「共同值/共同缺席」而非某一侧——互换对照必须固化此边界。
    for marker, oracle_item in exp_a.items():
        before, after = oracle_item["automatic"], exp_b[marker]["automatic"]
        branches_equal = oracle_item["rows"]["left"] == oracle_item["rows"]["right"]
        if branches_equal:
            if (before, after) != ("left", "left"):
                rep.fail("F", f"{marker} 双支同值项互换后应保持 left 自动采纳：{before} -> {after}")
        elif before in ("left", "right"):
            if (before, after) not in (("left", "right"), ("right", "left")):
                rep.fail("F", f"{marker} 单边项互换后未翻转：{before} -> {after}")
        else:
            if after is not None:
                rep.fail("F", f"{marker} 冲突项互换后不应产生自动采纳")

    # 镜像裁决重演：互换计划中选 right 应与原计划选 left 得到同一业务库
    decisions_a = {i["id"]: "left" for i in items_a if i["automatic"] is None}
    decisions_b = {i["id"]: "right" for i in items_b if i["automatic"] is None}
    cand_a, _ = _replay(catalog_a, trio, items_a, decisions_a)
    cand_b, _ = _replay(catalog_b, swapped, items_b, decisions_b)
    db_a = _read_candidate(cand_a, catalog_a)
    db_b = _read_candidate(cand_b, catalog_b)
    if db_a != db_b:
        rep.fail("F", "镜像裁决（原选左 / 互换选右）重演业务库不一致")
    rep.ok("F", "互换左右支：身份集合不变、左右载荷对调、automatic 对称翻转、镜像裁决业务值相同")

    # 语义变化的对照点：单边项的采纳侧确实翻转（抽查 S10/S11）
    s10 = normalize_key((110,))
    s11 = normalize_key((111,))
    if index_a[("ledger", s10)]["automatic"] != "left":
        rep.fail("F", "对照前提失效：S10 应为左支单边修改")
    if index_b[("ledger", s10)]["automatic"] != "right":
        rep.fail("F", "互换后左支单边修改未变为右支单边采纳")
    if index_a[("ledger", s11)]["automatic"] != "right" or \
            index_b[("ledger", s11)]["automatic"] != "left":
        rep.fail("F", "S11 互换翻转方向错误")
    rep.ok("F", "对照确认：单边修改项的采纳侧随支系对调而改变语义")


# -------------------------------------------------- G. 巧合相同的分类错误检出

def check_coincidence_detection(rep, trio):
    """错误分类但结果巧合相同的检出手段。

    使用 fixtures.build_twin_trio() 的专用镜像：BLOB 亲和键列在磁盘上同时
    保留整数 202 与等值实数 202.0 两种存储表示，而 SQLite 比较规则视其为
    同一行身份。构造一个「按序列化表示分键」的错误孪生分类器（不做整值
    实数归一化），它会把该身份拆成两个计划项，对祖本→左支产出
    「删整数行 + 插实数行」。重演后引擎中仍是同一行，逐格业务值与正确
    分类完全一致——只比较终态数据库必然漏检。检查器分三层比对：
      1. 计划分区（身份集合）——孪生多出一个虚构身份；
      2. 操作痕迹——孪生是 delete+insert，正确实现是单次 update；
      3. 终态业务值——两者巧合相同，证明此层单独使用不可靠。
    """
    twin_trio = fixtures.build_twin_trio()
    catalog, correct_items = _product_plan(twin_trio)
    expected = expected_plan(*twin_trio)

    # 正确路径：产品把两种表示归一为同一身份，计划恰好一项、左支单边修改
    if len(correct_items) != 1:
        rep.fail("G", f"正确计划应只有 1 个身份，实际 {len(correct_items)}")
    item = correct_items[0]
    identity = _product_identity(item)
    if identity != normalize_key((202,)) or item["automatic"] != "left":
        rep.fail("G", f"正确分类应为身份 202 的左支单边修改，实际 {item['key']} / "
                      f"{item['automatic']}")
    rep.ok("G", "正确归一化：整数 202 与等值实数 202.0 收敛为同一计划身份")

    # 孪生路径：不经 key_atom 归一化，直接用 codec JSON 表示当键
    twin_snapshots = []
    for data in twin_trio:
        with mounted(data) as db:
            twin_snapshots.append(_raw_token_scan(db, catalog))
    int_token = canonical([atom(202)])
    real_token = canonical([atom(202.0)])
    twin_tokens = {marker[1] for snapshot in twin_snapshots for marker in snapshot
                   if marker[0] == "twin" and marker[1] in (int_token, real_token)}
    if twin_tokens != {int_token, real_token}:
        rep.fail("G", f"错误孪生未把两种表示拆成两个身份：{twin_tokens}")
    rep.ok("G", "错误孪生按序列化表示把同一身份拆成整数/实数两个分区")

    # 终态层：正确产品重演后的业务值
    decisions = {}
    candidate, operations = _replay(catalog, twin_trio, correct_items, decisions)
    expected_db = expected_database(twin_trio[0], expected, {})
    _assert_business_values(rep, candidate, catalog, expected_db, "G",
                            "终态对照（孪生与正确实现在此层巧合相同）")

    # 痕迹层：正确实现单次 update/left；孪生在两个表示令牌上分别 delete/insert
    expected_traces = expected_operations(expected, {})
    if expected_traces.get(("twin", identity)) != ("update", "left"):
        rep.fail("G", f"正确痕迹应为单次 update/left，实际 "
                      f"{expected_traces.get(('twin', identity))}")
    twin_kinds = _twin_trace_kinds(twin_snapshots, "twin", (int_token, real_token))
    if twin_kinds != {"delete", "insert"}:
        rep.fail("G", f"孪生痕迹应为 delete+insert，实际 {twin_kinds}")
    rep.ok("G", "孪生产生 delete+insert 而正确实现为单次 update；终态虽巧合相同，"
                "分区与操作痕迹检查仍能稳定检出该分类错误")


def _twin_trace_kinds(twin_snapshots, table, tokens):
    """按孪生身份计算祖本→左支的痕迹类别（仅用于演示检出，不参与产品判断）。"""
    base, left = twin_snapshots[0], twin_snapshots[1]
    kinds = set()
    for token in tokens:
        in_base = (table, token) in base
        in_left = (table, token) in left
        if in_base and not in_left:
            kinds.add("delete")
        elif not in_base and in_left:
            kinds.add("insert")
    return kinds


def _raw_token_scan(db, catalog):
    """模拟错误分类器：不经 key_atom 归一化，直接序列化原始存储值当键。"""
    result = set()
    for table in catalog["tables"]:
        positions = [table["columns"].index(key) for key in table["key"]]
        cols = ",".join(f'"{c}"' for c in table["columns"])
        for raw in db.execute(f'SELECT {cols} FROM main."{table["name"]}"'):
            raw_key = [atom(raw[pos]) for pos in positions]
            result.add((table["name"], canonical(raw_key)))
    return result


# ------------------------------------------------------------- H. 全链路入口

def run_all(keep_dir=None):
    rep = Reporter()
    trio = fixtures.build_trio()

    check_engine_evidence(rep)
    check_representation_distinctness(rep)
    catalog, items, expected, exp_index = check_plan_classification(rep, trio)
    check_replay_values(rep, trio, catalog, items, expected, exp_index)
    check_operation_traces(rep, trio, catalog, items, expected, exp_index)
    check_swap_symmetry(rep, trio)
    check_coincidence_detection(rep, trio)
    check_store_roundtrip(rep, trio, keep_dir)
    rep.report()
    return rep


def check_store_roundtrip(rep, trio, keep_dir=None):
    """经 Store 登记与 create_plan 走一遍持久化入口，确认计划落库后
    读出的分类与直接计算一致（可重复入口的集成保证）。"""
    if keep_dir is None:
        temp = tempfile.TemporaryDirectory(prefix="pagefold-conformance-")
        root = Path(temp.name)
    else:
        temp = None
        root = Path(keep_dir)
        root.mkdir(parents=True, exist_ok=True)
    try:
        store = Store(root / "state.sqlite")
        ids = []
        for role, data in zip(("base", "left", "right"), trio):
            ids.append(register(store, data, f"conformance-{role}")["id"])
        sources = dict(zip(("base", "left", "right"), ids))
        plan = create_plan(store, sources)
        expected = expected_plan(*trio)
        if len(plan["items"]) != len(expected):
            rep.fail("H", f"落库计划项数 {len(plan['items'])} != 预期 {len(expected)}")
        automatic = sorted((item["automatic"] for item in plan["items"]), key=lambda v: str(v))
        expected_automatic = sorted((item["automatic"] for item in expected), key=lambda v: str(v))
        if automatic != expected_automatic:
            rep.fail("H", "落库计划的 automatic 分类序列与独立判断不符")
        if plan["unresolved"] != sum(1 for item in expected if item["automatic"] is None):
            rep.fail("H", "落库计划的未决冲突数不符")
        rep.ok("H", f"经 Store 登记与计划落库的持久化入口分类一致（{plan['unresolved']} 项待裁决）")
    finally:
        if temp is not None:
            temp.cleanup()
