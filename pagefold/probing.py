"""只读的冲突组合试算探索。

试算在内存中的祖本副本上重演候选裁决组合，复用 :mod:`pagefold.replay` 的
重建与约束校验逻辑，但不读取也不写入计划的裁决、版本或重演历史。探索在调用方
给定的预算内进行，返回每个组合的证据、可交付性差异分析以及尚未覆盖的组合。

每个冲突项有各自可用的角色集合（某角色在该项意味着行缺席时不可选），组合空间
是每项宽度相乘的混合基数空间；组合序号按冲突项顺序做混合基数编码。
"""
import json
import random

from .codec import digest, fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES
from .plans import expect_revision, get_plan, plan_content
from .replay import amendments, rebuild

MAX_BUDGET = 128
UNTESTED_SAMPLE = 8
ROLE_ORDER = ("base", "left", "right")


def conflict_items(items):
    return [item for item in items if item["automatic"] is None]


def normalize_domain(items, domain):
    if domain is None:
        requested = list(ROLES)
    else:
        if not isinstance(domain, list) or not domain or len(domain) != len(set(domain)):
            raise Problem("probe_domain", "试算选项域须是非空且不重复的角色数组。", 400)
        requested = [role for role in ROLE_ORDER if role in domain]
        if any(role not in ROLES for role in domain) or len(requested) != len(domain):
            raise Problem("probe_domain", "选项域只能包含 base、left、right 角色。", 400)
    # 行缺席（删除）也是合法选法：replay 选中缺席行即执行删除，故域宽保留该角色。
    widths = {item["id"]: list(requested) for item in items}
    return requested, widths


def normalize_assignments(items, widths, assignments):
    if assignments is None:
        return None
    if not isinstance(assignments, dict):
        raise Problem("probe_assignment", "指定组合须是 冲突条目 -> 角色 的对象。", 400)
    if set(assignments) != {item["id"] for item in items}:
        raise Problem("probe_assignment", "指定组合须覆盖全部冲突条目，且不能包含未知条目。", 400)
    chosen = {}
    for item in items:
        role = assignments[item["id"]]
        if role not in widths[item["id"]]:
            raise Problem("probe_assignment",
                          f"条目 {item['id']} 的选择 {role!r} 不在该项的试算选项内。", 400)
        chosen[item["id"]] = role
    return chosen

def encode(indexed, widths, choices):
    """混合基数编码：最后一个冲突项是最高位，序号在全空间内稠密且可复现。"""
    index, stride = 0, 1
    for item in indexed:
        index += widths[item["id"]].index(choices[item["id"]]) * stride
        stride *= len(widths[item["id"]])
    return index


def decode(indexed, widths, index):
    choices = {}
    for item in indexed:
        width = len(widths[item["id"]])
        choices[item["id"]] = widths[item["id"]][index % width]
        index //= width
    return choices


def total_space(indexed, widths):
    total = 1
    for item in indexed:
        total *= len(widths[item["id"]])
    return total


def run_trial(items, decisions, ancestor, catalog, indexed, widths, index):
    choices = decode(indexed, widths, index)
    merged = dict(decisions)
    merged.update(choices)
    entry = {"index": index, "assignment": [
        {"item": item["id"], "table": item["table"], "key": item["key"], "choice": choices[item["id"]]}
        for item in indexed]}
    try:
        operations = amendments(items, merged)
        candidate = rebuild(ancestor["body"], catalog, operations)
        entry.update(status="passed", changed=sum(map(len, operations.values())),
                     candidate_digest=digest(candidate))
    except Problem as exc:
        entry.update(status="blocked", problem=exc.as_dict())
    return entry


def schedule(indexed, widths, budget, anchor, explicit, rng):
    """分阶段调度试算序号，使预算内证据的覆盖面可解释。"""
    space = total_space(indexed, widths)
    planned, seen = [], set()

    def add(index):
        if index not in seen and len(seen) < budget:
            seen.add(index)
            planned.append(index)

    # 调用方显式指定的组合优先，保证预算 1 时只试算该组合。
    if explicit is not None:
        add(explicit)
    add(anchor)
    # 阶段二：单因素翻转，保证每个冲突项的每个候选选项至少出现一次。
    baseline = decode(indexed, widths, anchor)
    for item in indexed:
        for role in widths[item["id"]]:
            trial = dict(baseline)
            trial[item["id"]] = role
            add(encode(indexed, widths, trial))
    # 阶段三：从已试组合向外扩展单步邻居，暴露"只在另一选项配合下才成立"的依赖。
    frontier = list(planned)
    while frontier and len(seen) < budget:
        base_choices = decode(indexed, widths, frontier.pop(0))
        neighbors = []
        for item in indexed:
            for role in widths[item["id"]]:
                if role == base_choices[item["id"]]:
                    continue
                trial = dict(base_choices)
                trial[item["id"]] = role
                neighbors.append(encode(indexed, widths, trial))
        rng.shuffle(neighbors)
        for neighbor in neighbors:
            if neighbor not in seen and len(seen) < budget:
                seen.add(neighbor)
                planned.append(neighbor)
                frontier.append(neighbor)
    # 阶段四：预算仍有余量时确定性抽样。大空间下不得物化或洗牌整个序号区间，
    # 改用有界的伪随机探测，直到补足预算或达到尝试上限。
    if len(seen) < budget:
        probe_rng = random.Random(rng.random())
        attempts, cap = 0, max(1000, budget * 40)
        while len(seen) < budget and attempts < cap:
            attempts += 1
            add(probe_rng.randrange(space))
    return planned


def is_foreign_key(entry):
    return entry["status"] == "blocked" and entry.get("problem", {}).get("code") == "foreign_key"


def choices_of(entry):
    return {part["item"]: part["choice"] for part in entry["assignment"]}


def analyze(indexed, trials):
    """基于已试组合的单因素翻转对，判断哪些冲突项改变可交付性。"""
    flips = {item["id"]: [] for item in indexed}
    for pos, left in enumerate(trials):
        left_choices = choices_of(left)
        for right in trials[pos + 1:]:
            right_choices = choices_of(right)
            differing = [item["id"] for item in indexed
                         if left_choices[item["id"]] != right_choices[item["id"]]]
            if len(differing) == 1:
                flips[differing[0]].append((left, right))

    findings, neutral = [], []
    for item in indexed:
        status_changes = [(left, right) for left, right in flips[item["id"]]
                          if (left["status"] == "passed") != (right["status"] == "passed")]
        record = {"item": item["id"], "table": item["table"], "key": item["key"]}
        if status_changes:
            left, right = status_changes[0]
            findings.append({**record, "alters_deliverability": True,
                             "evidence": sorted((left["index"], right["index"])),
                             "deliverable_indices": sorted(
                                 entry["index"] for entry in (left, right) if entry["status"] == "passed"),
                             "blocked_indices": sorted(
                                 entry["index"] for entry in (left, right) if entry["status"] == "blocked")})
        else:
            neutral.append({**record, "alters_deliverability": False,
                            "observed_pairs": len(flips[item["id"]])})

    # 条件依赖：受阻组合在已试空间中没有单翻转即可通过的邻居时，说明需要多项
    # 配合；取距离最近的通过组合作为证据，并显式给出距离与涉及的冲突项。
    interactions = []
    blocked = [entry for entry in trials if entry["status"] == "blocked"]
    passed = [entry for entry in trials if entry["status"] == "passed"]
    limit = max(2, len(indexed) // 4)
    for blocked_entry in blocked:
        blocked_choices = choices_of(blocked_entry)
        distances = []
        for good in passed:
            differing = tuple(item["id"] for item in indexed
                              if blocked_choices[item["id"]] != choices_of(good)[item["id"]])
            distances.append((len(differing), differing, good))
        if not distances:
            continue
        distance, differing, good = min(distances, key=lambda record: record[0])
        if distance > limit:
            continue
        interactions.append({
            "blocked_index": blocked_entry["index"],
            "passed_index": good["index"],
            "distance": distance,
            "conditional": distance >= 2,
            "depends_on": sorted(differing),
            "problem": blocked_entry.get("problem"),
            "foreign_key": is_foreign_key(blocked_entry)})
    return findings, neutral, interactions


def coverage_report(indexed, widths, trials, space, seed):
    tested_indices = {entry["index"] for entry in trials}
    item_coverage = []
    for item in indexed:
        tried = {part["choice"] for entry in trials
                 for part in entry["assignment"] if part["item"] == item["id"]}
        available = widths[item["id"]]
        item_coverage.append({"item": item["id"], "table": item["table"], "key": item["key"],
                              "available_roles": available, "tested_roles":
                              [role for role in available if role in tried],
                              "complete": set(available) <= tried})
    # 成对覆盖率：线性扫描每个已试组合，收集其中实际共同出现的选项对。
    pair_total = 0
    for pos, left in enumerate(indexed):
        for right in indexed[pos + 1:]:
            pair_total += len(widths[left["id"]]) * len(widths[right["id"]])
    observed_pairs = set()
    for entry in trials:
        chosen = {part["item"]: part["choice"] for part in entry["assignment"]}
        for pos, left in enumerate(indexed):
            for right in indexed[pos + 1:]:
                observed_pairs.add((left["id"], right["id"],
                                    chosen[left["id"]], chosen[right["id"]]))
    pairs = observed_pairs
    untested_count = space - len(tested_indices)
    if untested_count == 0:
        untested, sample = [], []
    elif space <= MAX_BUDGET:
        untested, sample = sorted(set(range(space)) - tested_indices), []
    else:
        # 大空间不物化全部序号：用固定种子的确定性抽样给出可复现的未覆盖样例。
        rng = random.Random(seed)
        sample, guard = [], set()
        while len(sample) < min(UNTESTED_SAMPLE, untested_count) and len(guard) < space:
            index = rng.randrange(space)
            guard.add(index)
            if index not in tested_indices and index not in sample:
                sample.append(index)
        untested = []
    return {
        "exhaustive": untested_count == 0,
        "tested_combinations": len(tested_indices),
        "total_combinations": space,
        "untested_count": untested_count,
        "untested_indices": untested,
        "untested_sample": sample,
        "items": item_coverage,
        "pair_coverage": {"tested": len(pairs), "total": pair_total,
                          "complete": len(pairs) == pair_total}}


def empty_coverage(budget):
    return {"exhaustive": True, "tested_combinations": 0, "total_combinations": 0,
            "untested_count": 0, "untested_indices": [], "untested_sample": [],
            "items": [], "pair_coverage": {"tested": 0, "total": 0, "complete": True}}


def probe(store, plan_id, revision, budget, domain=None, assignments=None):
    """对计划执行只读试算，返回证据化探索报告，不产生任何持久化写入。"""
    if not isinstance(budget, int) or not 1 <= budget <= MAX_BUDGET:
        raise Problem("probe_budget", f"试算预算须是 1 至 {MAX_BUDGET} 之间的整数。", 400)
    with store.connect() as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        items, decisions = plan_content(plan)
        ancestor = get_image(db, plan["base"], body=True)
        catalog = json.loads(ancestor["catalog"])
    indexed = conflict_items(items)
    if not indexed:
        return {"id": plan_id, "revision": revision, "read_only": True, "conflicts": 0,
                "domain": list(ROLES), "budget": budget, "budget_used": 0,
                "passed_combinations": 0, "blocked_combinations": 0, "trials": [],
                "findings": [], "neutral_items": [], "interactions": [],
                "coverage": empty_coverage(budget)}
    requested, widths = normalize_domain(indexed, domain)
    chosen = normalize_assignments(indexed, widths, assignments)
    space = total_space(indexed, widths)
    effective_budget = min(budget, space)
    anchor_choices = {item["id"]: widths[item["id"]][0] for item in indexed}
    anchor = encode(indexed, widths, anchor_choices)
    explicit = encode(indexed, widths, chosen) if chosen is not None else None
    seed = fingerprint([plan_id, effective_budget, requested])
    rng = random.Random(seed)
    order = schedule(indexed, widths, effective_budget, anchor, explicit, rng)
    trials = [run_trial(items, decisions, ancestor, catalog, indexed, widths, index)
              for index in order]
    findings, neutral, interactions = analyze(indexed, trials)
    return {
        "id": plan_id,
        "revision": revision,
        "read_only": True,
        "conflicts": len(indexed),
        "domain": requested,
        "budget": budget,
        "budget_used": len(trials),
        "passed_combinations": sum(entry["status"] == "passed" for entry in trials),
        "blocked_combinations": sum(entry["status"] == "blocked" for entry in trials),
        "trials": trials,
        "findings": findings,
        "neutral_items": neutral,
        "interactions": interactions,
        "coverage": coverage_report(indexed, widths, trials, space, seed)}
