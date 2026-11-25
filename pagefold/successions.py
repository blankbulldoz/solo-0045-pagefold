import json
import uuid

from .codec import canonical
from .errors import Problem
from .images import get_image
from .merge import ROLES, calculate
from .plans import expect_revision, get_plan, plan_content
from .rows import read_image
from .sqlite_image import mounted

# 承接核对状态：
# retained          旧手工选择所指来源行未变，继续有效，已写入新计划
# needs_review      同一键仍是冲突，但旧选择来源行已变化，必须重新裁决
# automatic         新祖本下该键已能自动采纳，手工选择不再适用（自动值与旧选择可能不同）
# dropped           该键在新三方下不再有差异，旧选择随条目一起消失
CARRY_STATES = ("retained", "needs_review", "automatic", "dropped")


def snapshots_for(images):
    result = []
    for image in images:
        catalog = json.loads(image["catalog"])
        with mounted(image["body"]) as snapshot:
            result.append(read_image(snapshot, catalog))
    return result


def entry_key(item):
    return item["table"], canonical(item["key"])


def build_report(old_items, old_decisions, new_items):
    """逐条核对旧手工选择在新祖本下的去向。"""
    new_index = {}
    for item in new_items:
        new_index.setdefault(entry_key(item), item)
    report = []
    for item in old_items:
        choice = old_decisions.get(item["id"])
        if choice is None:
            continue  # 旧计划里未手工裁决的冲突没有可承接的选择
        current = new_index.get(entry_key(item))
        if current is None:
            status, reason = "dropped", "新三方下该键已无差异，旧条目消失。"
        elif current["automatic"] is not None:
            same = current[current["automatic"]] == item[choice]
            status = "automatic"
            reason = ("系统自动采纳与旧选择一致。" if same else
                      "差异已自动化，自动采纳的来源与旧手工选择不同。")
        elif current[choice] == item[choice]:
            status, reason = "retained", "旧选择来源行在更正后的祖本下未变化。"
        else:
            status, reason = "needs_review", "旧选择来源行已变化，需要重新核对。"
        report.append({"id": item["id"], "table": item["table"], "key": item["key"],
                       "previous_choice": choice, "status": status,
                       "new_item_id": current["id"] if current is not None else None, "reason": reason})
    return report


def create_succession(db, plan, sources):
    """在已开启的写事务内建立承接计划，返回新计划行与承接报告。"""
    images = [get_image(db, sources[role], body=True) for role in ROLES]
    catalogs = [json.loads(image["catalog"]) for image in images]
    if len({catalog["signature"] for catalog in catalogs}) != 1:
        raise Problem("schema_mismatch", "承接镜像的模式记录不一致。", 409)
    new_items = calculate(*snapshots_for(images))
    old_items, old_decisions = plan_content(plan)
    report = build_report(old_items, old_decisions, new_items)
    new_index = {entry_key(item): item for item in new_items}
    carried = {}
    for entry in report:
        if entry["status"] != "retained":
            continue
        target = new_index[(entry["table"], canonical(entry["key"]))]
        carried[target["id"]] = entry["previous_choice"]
    successor_id = uuid.uuid4().hex
    db.execute("INSERT INTO plans(id,base,left_id,right_id,items,decisions,revision) VALUES(?,?,?,?,?,?,?)",
               (successor_id, *[sources[role] for role in ROLES],
                canonical(new_items), canonical(carried), 1 if carried else 0))
    summary = {state: 0 for state in CARRY_STATES}
    for entry in report:
        summary[entry["status"]] += 1
    full_report = {"predecessor": plan["id"], "successor": successor_id,
                   "summary": summary, "entries": report}
    db.execute("INSERT INTO successions VALUES(?,?,?)",
               (plan["id"], successor_id, canonical(full_report)))
    return successor_id, full_report


def archive_with_succession(store, plan_id, revision, sources=None, drill_fail=False):
    """归档旧计划；sources 给定时在同一事务内建立承接计划并逐条核对选择。"""
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        if plan["state"] == "archived":
            if sources is not None:
                raise Problem("closed_plan", "归档后的计划不能再建立承接关系。", 409)
            result = {"id": plan_id, "revision": revision, "state": "archived"}
            existing = get_succession(db, plan_id)
            if existing is not None:
                result.update(successor=existing["successor_id"], report=json.loads(existing["report"]))
            return result
        report = None
        successor_id = None
        if sources is not None:
            successor_id, report = create_succession(db, plan, sources)
            if drill_fail:
                # 演练专用：模拟状态库在归档提交前失效，事务必须整体回滚。
                raise Problem("drill_failure", "演练注入的写入失败，承接事务应整体回滚。", 500)
        db.execute("UPDATE plans SET state='archived' WHERE id=?", (plan_id,))
        result = {"id": plan_id, "revision": revision, "state": "archived"}
        if successor_id is not None:
            result.update(successor=successor_id, report=report)
        return result


def get_succession(db, plan_id):
    return db.execute("SELECT * FROM successions WHERE predecessor_id=? OR successor_id=?",
                      (plan_id, plan_id)).fetchone()


def succession_view(db, plan_id):
    row = get_succession(db, plan_id)
    if row is None:
        return None
    relation = "predecessor" if row["predecessor_id"] == plan_id else "successor"
    other = row["successor_id"] if relation == "predecessor" else row["predecessor_id"]
    return {"relation": relation, "predecessor": row["predecessor_id"],
            "successor": row["successor_id"], "other_plan": other,
            "report": json.loads(row["report"])}
