import json
import sqlite3

from .codec import canonical, digest, fingerprint
from .errors import Problem
from .images import get_image
from .merge import ROLES, selected
from .plans import expect_open, expect_revision, get_plan, plan_content
from .replay import amendments, rebuild
from .rows import read_image
from .sqlite_image import mounted


def get_attempt(db, plan_id, revision):
    return db.execute("SELECT result,candidate FROM attempts WHERE plan_id=? AND revision=?",
                      (plan_id, revision)).fetchone()


def list_attempts(db, plan_id):
    get_plan(db, plan_id)
    return [json.loads(row[0]) for row in db.execute(
        "SELECT result FROM attempts WHERE plan_id=? ORDER BY revision DESC", (plan_id,))]


def rehearse(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)
        previous = get_attempt(db, plan_id, revision)
        if previous:
            return json.loads(previous["result"])
        items, decisions = plan_content(plan)
        # 留存该版每条计划项实际采用的角色，使“当时的选择”成为记录事实，
        # 而不是只能用计划当前的 decisions 反推。
        choices = {item["id"]: selected(item, decisions)[0] for item in items}
        operations = amendments(items, decisions)
        ancestor = get_image(db, plan["base"], body=True)
        candidate = None
        result = {"id": plan_id, "revision": revision, "changed": sum(map(len, operations.values())),
                  "choices": choices, "decision_digest": fingerprint(decisions)}
        try:
            candidate = rebuild(ancestor["body"], json.loads(ancestor["catalog"]), operations)
            result.update(status="passed", candidate_digest=digest(candidate), size=len(candidate))
        except Problem as exc:
            problem = exc.as_dict()
            if exc.context:
                problem["context"] = exc.context
            result.update(status="blocked", problem=problem)
        except sqlite3.Error as exc:
            result.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
        db.execute("INSERT INTO attempts VALUES(?,?,?,?)", (plan_id, revision, canonical(result), candidate))
        return result


def _candidate_snapshot(candidate, catalog):
    with mounted(candidate) as snapshot:
        return read_image(snapshot, catalog)


def _resolve_role(item, ctx):
    """返回 (唯一角色或 None, 全部匹配角色, 证据级别)。"""
    if item is None:
        return None, [], "outside"
    recorded = ctx["recorded"]
    if recorded is not None:
        role = recorded.get(item["id"])
        if role in ROLES:
            return role, [role], "recorded"
    snapshot = ctx["snapshot"]
    if snapshot is not None:
        actual = snapshot.get(item["table"], {}).get(canonical(item["key"]))
        matches = [role for role in ROLES if canonical(item[role]) == canonical(actual)]
        if matches:
            return (matches[0] if len(matches) == 1 else None), matches, "candidate"
        return None, [], "candidate_unmatched"
    role = item["automatic"] or ctx["decisions"].get(item["id"])
    if role in ROLES:
        return role, [role], "current"
    return None, [], "missing"


def _build_cell(item, table, key_token, ctx):
    role, roles, evidence = _resolve_role(item, ctx)
    source = ctx["sources"].get(role) if role else None
    snapshot = ctx["snapshot"]
    actual_known = snapshot is not None
    actual_row = snapshot.get(table, {}).get(key_token) if actual_known else None
    if actual_known:
        row, row_evidence = actual_row, "proven"
    elif role is not None and item is not None:
        row, row_evidence = item[role], "inferred"
    else:
        row, row_evidence, = None, "unknown"
    intended_row = item[role] if (item is not None and role is not None) else None
    amended = None
    if item is not None:
        if actual_known:
            amended = canonical(actual_row) != canonical(item["base"])
        elif role is not None:
            amended = canonical(item[role]) != canonical(item["base"])
    failed_operation = False
    context = ctx["problem_context"]
    if context and context.get("table") == table:
        if item is not None and context.get("id") == item["id"]:
            failed_operation = True
        elif context.get("key") is not None and canonical(context["key"]) == key_token:
            failed_operation = True
    return {
        "revision": ctx["revision"], "status": ctx["status"],
        "choice": role, "choice_roles": roles, "choice_evidence": evidence,
        "source_id": source["id"] if source else None,
        "source_label": source["label"] if source else None,
        "row": row, "actual_row": actual_row, "actual_known": actual_known,
        "intended_row": intended_row, "row_evidence": row_evidence,
        "amended": amended, "failed_operation": failed_operation,
    }


def _verify_reproduction(db, plan, items, ctx):
    """按留存的当时选择在内存中重放，检验来源与操作是否可重现。

    不写状态库、不产生正式重演记录，结论只作为离线复核证据。
    """
    recorded = ctx["recorded"]
    if recorded is None:
        return {"supported": False, "reason": "choices_not_recorded",
                "note": "这条旧记录没有留存当时的选择，无法按记录重放。"}
    try:
        operations = amendments(items, recorded)
        ancestor = get_image(db, plan["base"], body=True)
        data = rebuild(ancestor["body"], json.loads(ancestor["catalog"]), operations)
    except Problem as exc:
        original = ctx["problem"] or {}
        same_point = False
        if exc.context and ctx["problem_context"]:
            same_point = exc.context.get("id") == ctx["problem_context"].get("id")
        return {"supported": True, "replayed": "blocked", "problem": exc.as_dict(),
                "reproduced": ctx["status"] == "blocked" and exc.code == original.get("code"),
                "same_problem_code": exc.code == original.get("code"), "same_block_point": same_point}
    except sqlite3.Error as exc:
        return {"supported": True, "replayed": "blocked",
                "problem": {"code": "sqlite_replay", "detail": str(exc)},
                "reproduced": ctx["status"] == "blocked"}
    except Exception as exc:  # 复核是附加能力，绝不能让整个比较页 500
        return {"supported": True, "replayed": "error",
                "problem": {"code": "reproduce_failed", "detail": str(exc)},
                "reproduced": False}
    actual_digest = digest(data)
    if ctx["status"] == "passed":
        return {"supported": True, "replayed": "passed", "candidate_digest": actual_digest,
                "reproduced": actual_digest == ctx["candidate_digest"]}
    return {"supported": True, "replayed": "passed", "candidate_digest": actual_digest,
            "reproduced": False, "note": "按留存选择本次重放通过，与原受阻记录不一致。"}


def compare_attempts(db_or_store, plan_id, a_revision=None, b_revision=None, verify=False, **kwargs):
    """比较同一计划两版重演记录的公开入口。

    同时接受状态库连接（与 ``list_attempts(db, ...)`` 一致）或 :class:`Store`；
    版本参数兼容常见关键字命名（old/new、from/to、revision_a/b）。
    """
    if a_revision is None:
        a_revision = kwargs.get("old_revision", kwargs.get("from_revision",
                                kwargs.get("revision_a", kwargs.get("rev_a", kwargs.get("left")))))
    if b_revision is None:
        b_revision = kwargs.get("new_revision", kwargs.get("to_revision",
                                kwargs.get("revision_b", kwargs.get("rev_b", kwargs.get("right")))))
    if a_revision is None or b_revision is None:
        raise Problem("compare_revisions", "请提供要比较的两版重演记录。", 400)
    a_revision, b_revision = int(a_revision), int(b_revision)
    if hasattr(db_or_store, "connect") and not hasattr(db_or_store, "execute"):
        with db_or_store.connect() as db:
            return _compare_attempts(db, plan_id, a_revision, b_revision, verify)
    return _compare_attempts(db_or_store, plan_id, a_revision, b_revision, verify)


def _compare_attempts(db, plan_id, a_revision, b_revision, verify=False):
    """比较同一计划的两版重演记录，关联计划项、当时选择与候选实物。

    证据分层：
    - passed 侧有留存候选字节，逐行重读得到的是“已证明”的实际行；
    - blocked 侧没有候选，只能依据留存选择（新记录）或当前裁决
      （旧记录的推断）给出目标行，属于“待重演的推断”；
    - 更早期的记录可能没有 choices 快照：通过侧仍可由候选反查来源角色，
      受阻侧则明确呈现为选择缺失，而不是伪造历史选择。
    """
    plan = get_plan(db, plan_id)
    items, current_decisions = plan_content(plan)
    item_by_id = {item["id"]: item for item in items}
    item_by_key = {(item["table"], canonical(item["key"])): item for item in items}
    base_image = get_image(db, plan["base"])
    catalog = json.loads(base_image["catalog"])
    sources = {}
    for role, column in (("base", "base"), ("left", "left_id"), ("right", "right_id")):
        image = get_image(db, plan[column])
        sources[role] = {"id": image["id"], "label": image["label"]}

    contexts = []
    sides = []
    for revision in (a_revision, b_revision):
        attempt = get_attempt(db, plan_id, revision)
        if attempt is None:
            raise Problem("unknown_attempt", f"找不到第 {revision} 版重演记录。", 404)
        result = json.loads(attempt["result"])
        snapshot = None
        snapshot_error = None
        candidate_intact = None
        if result.get("status") == "passed" and attempt["candidate"] is not None:
            candidate_intact = digest(attempt["candidate"]) == result.get("candidate_digest")
            try:
                snapshot = _candidate_snapshot(attempt["candidate"], catalog)
            except (Problem, sqlite3.Error) as exc:
                snapshot_error = exc.as_dict() if isinstance(exc, Problem) else {
                    "code": "candidate_unreadable", "detail": str(exc)}
        recorded = result.get("choices")
        recorded = recorded if isinstance(recorded, dict) else None
        problem = result.get("problem") if isinstance(result.get("problem"), dict) else None
        ctx = {
            "revision": revision, "result": result, "status": result.get("status"),
            "candidate_digest": result.get("candidate_digest"),
            "problem": problem, "problem_context": (problem or {}).get("context"),
            "recorded": recorded, "decisions": current_decisions, "snapshot": snapshot,
            "sources": sources,
        }
        contexts.append(ctx)
        side = {
            "revision": revision, "status": result.get("status"),
            "passed": result.get("status") == "passed", "changed": result.get("changed"),
            "candidate_digest": result.get("candidate_digest"),
            "candidate_size": result.get("size"), "has_candidate": attempt["candidate"] is not None,
            "problem": problem, "choices_recorded": recorded is not None,
            "decision_digest": result.get("decision_digest"), "snapshot_error": snapshot_error,
            "candidate_intact": candidate_intact,
        }
        side["reproduction"] = _verify_reproduction(db, plan, items, ctx) if verify else {
            "supported": recorded is not None}
        sides.append(side)

    ctx_a, ctx_b = contexts

    universe = {}
    for item in items:
        universe[(item["table"], canonical(item["key"]))] = item
    # 计划项覆盖一切可能的操作行；仅当两个候选都在、且出现计划外的实际差异时
    # （按构造不应发生），才把这些键补入集合，避免把整库未变动行塞进比较结果。
    snapshot_a, snapshot_b = ctx_a["snapshot"], ctx_b["snapshot"]
    if snapshot_a is not None and snapshot_b is not None:
        for table in set(snapshot_a) | set(snapshot_b):
            rows_a, rows_b = snapshot_a.get(table, {}), snapshot_b.get(table, {})
            for key_token in set(rows_a) | set(rows_b):
                identity = (table, key_token)
                if identity not in universe and canonical(rows_a.get(key_token)) != canonical(rows_b.get(key_token)):
                    universe[identity] = item_by_key.get(identity)

    rows = []
    for (table, key_token), item in sorted(universe.items(), key=lambda pair: (pair[0][0], pair[0][1])):
        cell_a = _build_cell(item, table, key_token, ctx_a)
        cell_b = _build_cell(item, table, key_token, ctx_b)
        if cell_a["row_evidence"] == "unknown" or cell_b["row_evidence"] == "unknown":
            kind = "undetermined"
        else:
            same_row = canonical(cell_a["row"]) == canonical(cell_b["row"])
            if same_row:
                kind = "same"
            elif cell_a["row"] is None:
                kind = "added"
            elif cell_b["row"] is None:
                kind = "removed"
            else:
                kind = "modified"
        if cell_a["actual_known"] and cell_b["actual_known"]:
            proven_change = canonical(cell_a["actual_row"]) != canonical(cell_b["actual_row"])
        else:
            proven_change = None
        choice_known_a = cell_a["choice_evidence"] in {"recorded", "candidate", "current"} and cell_a["choice"]
        choice_known_b = cell_b["choice_evidence"] in {"recorded", "candidate", "current"} and cell_b["choice"]
        choice_changed = bool(choice_known_a and choice_known_b and cell_a["choice"] != cell_b["choice"])
        blocker = None
        for side_name, cell in (("a", cell_a), ("b", cell_b)):
            if cell["failed_operation"]:
                blocker = {"side": side_name, "context": (ctx_a if side_name == "a" else ctx_b)["problem_context"]}
        rows.append({
            "item_id": item["id"] if item else None, "in_plan": item is not None,
            "table": table, "key": json.loads(key_token), "kind": kind,
            "changed": kind != "same", "proven_change": proven_change,
            "choice_changed": choice_changed, "blocker": blocker,
            "old_row": cell_a["row"], "new_row": cell_b["row"],
            "old_choice": cell_a["choice"], "new_choice": cell_b["choice"],
            "a": cell_a, "b": cell_b,
        })

    changes = [row for row in rows
               if row["kind"] != "same" or row["choice_changed"] or row["blocker"] is not None]

    digest_a, digest_b = ctx_a["candidate_digest"], ctx_b["candidate_digest"]
    same_candidate = bool(digest_a and digest_a == digest_b)
    same_selection = bool(ctx_a["recorded"] is not None and ctx_b["recorded"] is not None
                          and canonical(ctx_a["recorded"]) == canonical(ctx_b["recorded"]))
    statuses = {ctx["status"] for ctx in contexts}
    if same_candidate:
        state = "same_candidate"
    elif statuses == {"passed"}:
        state = "candidate_diff"
    elif "passed" in statuses and len(statuses) == 2:
        state = "passed_blocked"
    elif statuses == {"blocked"}:
        state = "both_blocked"
    else:
        state = "incomplete"

    def count_evidence(side_index, evidence):
        return sum(row[("a", "b")[side_index]]["choice_evidence"] == evidence for row in rows)

    summary = {
        "total": len(rows), "changed": len(changes),
        "proven_changes": sum(row["proven_change"] is True for row in rows),
        "inferred_rows": sum(row["kind"] != "same" and (
            row["a"]["row_evidence"] == "inferred" or row["b"]["row_evidence"] == "inferred")
            for row in changes),
        "missing_choices": sum(
            cell["choice_evidence"] == "missing" for row in rows for cell in (row["a"], row["b"])),
        "recorded_choices": sum(count_evidence(i, "recorded") for i in (0, 1)),
        "candidate_choices": sum(count_evidence(i, "candidate") for i in (0, 1)),
        "current_choices": sum(count_evidence(i, "current") for i in (0, 1)),
    }

    return {
        "plan": plan_id, "id": plan_id,
        "a_revision": a_revision, "b_revision": b_revision,
        "a": sides[0], "b": sides[1], "old": sides[0], "new": sides[1], "sides": sides,
        "state": state, "same_candidate": same_candidate, "identical_candidate": same_candidate,
        "same_selection": same_selection,
        "candidate_digests": {"a": digest_a, "b": digest_b},
        "rows": rows, "changes": changes, "diff": changes,
        "summary": summary, "sources": sources, "schema_signature": catalog["signature"],
        "reproduction_guide": {
            "method": "amendments(items, choices) → rebuild(base 字节, catalog, operations)",
            "verify_query": "?verify=1",
            "idempotent_rehearse": "同一 revision 的 POST /rehearse 直接返回已留存记录与同一候选",
        },
    }
