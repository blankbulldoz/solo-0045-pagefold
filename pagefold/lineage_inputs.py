"""并列试算素材：同一对分支、两个有凭据的候选祖本。

两套输入（alpha / beta）使用同一份模式，复合主键 (band, seq) 的取值区间相互重叠，
beta 套把左右分支的角色整体镜像，便于追溯角色与模式在比较中的去向。

每个键的三方真值（取值或 X=缺席）决定它在某个祖本视角下的处置：

==========  ============================  ============================
主键         候选祖本 A 视角                候选祖本 B 视角
==========  ============================  ============================
seq1 冲突键  双边不同 → 待决                双边不同 → 待决（保持）
seq2/3 翻转  单边跟随 → 自动 left/right     祖本已含一侧改值 → 角色翻转
seq4 删除键  祖本有、双方删 → 自动删除      三方同缺席 → 省略（自动项消失）
seq6 B 平衡  三方同值 → 省略（无差异）      祖本独旧、双方同新 → 自动
seq8 待决化解 三边全异 → 待决               祖本取一支值 → 待决变自动
seq5/7 稳定  三方同值 → 省略                三方同值 → 省略（无差异）
==========  ============================  ============================

固定同一对分支时，单个主键不可能在一个祖本下是待决、在另一个祖本下因三方同值而
省略：待决要求左右不同，省略要求左右相同。因此“待决消失”在素材里以 seq8 的
待决→自动（未决计数下降）来体现，而真正从计划中消失的是 seq4 的自动删除项；
对齐视图同时给出 absent / automatic / conflict 三种状态，逐键可区分。

两个视角的计划项总数都为 5（seq4、seq8 只在 A 出现，seq6 只在 B 出现），
自动采纳角色会翻转、未决计数会变化，总数相同但条目集合与实质均不同。
所有镜像在内存中生成并通过与正式登记相同的约束规则，可直接上传到工作区。
"""
import sqlite3

SCHEMA = """
CREATE TABLE ledger(
    band TEXT NOT NULL,
    seq INTEGER NOT NULL,
    mark TEXT NOT NULL,
    note TEXT,
    weight REAL CHECK(weight >= 0),
    PRIMARY KEY(band, seq)
) WITHOUT ROWID;
CREATE INDEX ledger_weight ON ledger(weight);
"""

# (band, seq, mark, note, weight)；mark 前缀说明取值来源，便于人工追溯。
ALPHA = {
    "base_a": [  # 候选祖本 A：含稍后双方都删除的行 (alpha, 4)
        ("alpha", 1, "A-base", "双边冲突键", 1.0),
        ("alpha", 2, "A-base", "左支单边改，右支不动", 2.0),
        ("alpha", 3, "A-base", "右支单边改，左支不动", 3.0),
        ("alpha", 4, "A-base", "双方稍后都删除的行", 4.0),
        ("alpha", 5, "A-same", "三方同值的无差异行", 5.0),
        ("alpha", 6, "B-both", "双方一致的新值", 6.5),
        ("alpha", 7, "A-keep", "三方都不变的稳定行", 7.0),
        ("alpha", 8, "A-base8", "A 下三边全异待决，B 祖本取右支值后自动", 8.0),
    ],
    "base_b": [  # 候选祖本 B：含双方都没变的行；不含被双方删除的行
        ("alpha", 1, "A-base", "双边冲突键", 1.0),
        ("alpha", 2, "A-left", "左支改值", 2.0),
        ("alpha", 3, "A-right", "右支改值", 3.0),
        ("alpha", 5, "A-same", "三方同值的无差异行", 5.0),
        ("alpha", 6, "B-own", "B 独有旧值，双方一致改成新值", 6.0),
        ("alpha", 7, "A-keep", "三方都不变的稳定行", 7.0),
        ("alpha", 8, "A-right8", "右支改值，B 祖本取此值", 8.2),
    ],
    "left": [
        ("alpha", 1, "A-left", "左支覆写", 11.0),
        ("alpha", 2, "A-left", "左支改值", 2.0),
        ("alpha", 3, "A-base", "右支单边改，左支不动", 3.0),
        ("alpha", 5, "A-same", "三方同值的无差异行", 5.0),
        ("alpha", 6, "B-both", "双方一致的新值", 6.5),
        ("alpha", 7, "A-keep", "三方都不变的稳定行", 7.0),
        ("alpha", 8, "A-left8", "左支改值，与右支不同", 8.1),
    ],
    "right": [
        ("alpha", 1, "A-right", "右支覆写", 12.0),
        ("alpha", 2, "A-base", "左支单边改，右支不动", 2.0),
        ("alpha", 3, "A-right", "右支改值", 3.0),
        ("alpha", 5, "A-same", "三方同值的无差异行", 5.0),
        ("alpha", 6, "B-both", "双方一致的新值", 6.5),
        ("alpha", 7, "A-keep", "三方都不变的稳定行", 7.0),
        ("alpha", 8, "A-right8", "右支改值，B 祖本取此值", 8.2),
    ],
}

# beta 套：alpha 套的左右角色镜像，band 换成 beta、seq 区间平移后仍与 alpha 重叠。
BETA = {
    "base_a": [
        ("beta", 1, "B-base", "双边冲突键", 1.0),
        ("beta", 2, "B-base", "右支单边改，左支不动", 2.0),
        ("beta", 3, "B-base", "左支单边改，右支不动", 3.0),
        ("beta", 4, "B-base", "双方稍后都删除的行", 4.0),
        ("beta", 5, "B-same", "三方同值的无差异行", 5.0),
        ("beta", 6, "A-both", "双方一致的新值", 6.5),
        ("beta", 7, "B-keep", "三方都不变的稳定行", 7.0),
        ("beta", 8, "B-base8", "镜像下 A 三边全异待决，B 取左支值后自动", 8.0),
    ],
    "base_b": [
        ("beta", 1, "B-base", "双边冲突键", 1.0),
        ("beta", 2, "B-right", "右支改值", 2.0),
        ("beta", 3, "B-left", "左支改值", 3.0),
        ("beta", 5, "B-same", "三方同值的无差异行", 5.0),
        ("beta", 6, "A-own", "B 独有旧值，双方一致改成新值", 6.0),
        ("beta", 7, "B-keep", "三方都不变的稳定行", 7.0),
        ("beta", 8, "B-left8", "左支改值，B 祖本取此值", 8.1),
    ],
    # alpha 中“左支做的改动”在 beta 中由右支完成，反之亦然。
    "left": [
        ("beta", 1, "B-left", "镜像后由左支持有覆写", 11.0),
        ("beta", 2, "B-base", "右支单边改，左支不动", 2.0),
        ("beta", 3, "B-left", "左支改值", 3.0),
        ("beta", 5, "B-same", "三方同值的无差异行", 5.0),
        ("beta", 6, "A-both", "双方一致的新值", 6.5),
        ("beta", 7, "B-keep", "三方都不变的稳定行", 7.0),
        ("beta", 8, "B-left8", "左支改值，B 祖本取此值", 8.1),
    ],
    "right": [
        ("beta", 1, "B-right", "镜像后由右支持有覆写", 12.0),
        ("beta", 2, "B-right", "右支改值", 2.0),
        ("beta", 3, "B-base", "左支单边改，右支不动", 3.0),
        ("beta", 5, "B-same", "三方同值的无差异行", 5.0),
        ("beta", 6, "A-both", "双方一致的新值", 6.5),
        ("beta", 7, "B-keep", "三方都不变的稳定行", 7.0),
        ("beta", 8, "B-right8", "右支改值，与左支不同", 8.2),
    ],
}

LABELS = {
    ("alpha", "base_a"): "α 候选祖本 A（含双方删除行）",
    ("alpha", "base_b"): "α 候选祖本 B（含双方未改行）",
    ("alpha", "left"): "α 左支",
    ("alpha", "right"): "α 右支",
    ("beta", "base_a"): "β 候选祖本 A（含双方删除行）",
    ("beta", "base_b"): "β 候选祖本 B（含双方未改行）",
    ("beta", "left"): "β 左支",
    ("beta", "right"): "β 右支",
}


def build_image(rows):
    db = sqlite3.connect(":memory:")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO ledger VALUES(?,?,?,?,?)", rows)
        db.commit()
        return db.serialize()
    finally:
        db.close()


def lineage_images():
    """返回 {镜像键: (标签, SQLite 字节)}，键如 'alpha_base_a'。"""
    result = {}
    for suite, tables in (("alpha", ALPHA), ("beta", BETA)):
        for name, rows in tables.items():
            result[f"{suite}_{name}"] = (LABELS[(suite, name)], build_image(rows))
    return result


def suite_sources(suite):
    """返回某一套的候选祖本键与左右分支键。"""
    return {
        "bases": [(f"{suite} 祖本 A", f"{suite}_base_a"),
                  (f"{suite} 祖本 B", f"{suite}_base_b")],
        "left": f"{suite}_left",
        "right": f"{suite}_right",
    }
