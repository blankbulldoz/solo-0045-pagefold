"""两版重演记录比较的公开入口与参数解析。

核心计算位于 :mod:`pagefold.attempts`（重演历史模块），这里保留一个语义化
的 ``pagefold.compare`` 导入位置，并统一 HTTP 与页面共用的版本参数规则。
"""

from .attempts import compare_attempts

__all__ = ["compare_attempts", "diff_attempts", "attempt_diff", "build_comparison",
           "requested_pair", "wants_verify", "resolve_pair"]

# 常见近义命名，方便不同入口按习惯导入。
diff_attempts = compare_attempts
attempt_diff = compare_attempts
build_comparison = compare_attempts

# 兼容不同调用习惯的查询参数名。
A_KEYS = ("a", "from", "old", "r1", "ra", "left", "first", "base", "base_revision",
          "revision_a", "rev_a", "old_revision", "from_revision", "oldRevision", "revisionA")
B_KEYS = ("b", "to", "new", "r2", "rb", "right", "second", "target", "against", "other",
          "target_revision", "revision_b", "rev_b", "new_revision", "to_revision",
          "newRevision", "revisionB")
VERIFY_KEYS = ("verify", "reproduce", "recheck", "check")
TRUE_VALUES = {"1", "true", "yes", "on", "check"}


def _first(query, keys):
    for key in keys:
        value = query.get(key)
        if value is not None and value != "":
            return value
    return None


def requested_pair(query):
    """从查询串取两个版本号；缺省或非法时交给调用方处理。

    返回 ``(a, b)``，未提供的一侧为 ``None``。格式错误抛 400。
    除独立的 a/b 参数外，也接受 ``pair=1,2``、``revisions=1,2`` 这类合并写法。
    """
    from .inputs import unsigned

    raw_a, raw_b = _first(query, A_KEYS), _first(query, B_KEYS)
    if raw_a is None and raw_b is None:
        paired = _first(query, ("pair", "revisions", "compare", "ids", "range"))
        if paired is not None:
            bits = str(paired).replace(":", ",").replace("-", ",").split(",")
            bits = [bit.strip() for bit in bits if bit.strip()]
            if len(bits) >= 2:
                raw_a, raw_b = bits[0], bits[1]
    a = unsigned(raw_a, "a") if raw_a is not None else None
    b = unsigned(raw_b, "b") if raw_b is not None else None
    return a, b


def wants_verify(query):
    value = _first(query, VERIFY_KEYS)
    return value is not None and value.lower() in TRUE_VALUES


def resolve_pair(query, revisions):
    """页面入口使用：补齐缺失的版本选择。

    ``revisions`` 为按时间倒序的版本号列表。规则：
    - 两侧都缺：取最近两版（只有一版时两侧都用它，可展示同一候选）；
    - 只给一侧：另一侧取相邻的更早/更晚版本，没有相邻则与自身对照。
    返回 ``(a, b)``；没有任何重演记录时返回 ``(None, None)``。
    """
    a, b = requested_pair(query)
    if not revisions:
        return None, None
    descending = sorted(revisions, reverse=True)
    if a is None and b is None:
        b = descending[0]
        older = [rev for rev in descending if rev < b]
        a = older[0] if older else b
    elif a is None:
        older = [rev for rev in descending if rev < b]
        a = older[0] if older else b
    elif b is None:
        newer = [rev for rev in descending if rev > a]
        b = newer[-1] if newer else a
    return a, b
