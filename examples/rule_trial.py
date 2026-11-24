"""Demonstrate dry-run row-selection rules and human-adjudicated writes.

The same four-conflict batch exercises broad coverage, overlapping opinions,
priority override, and a NULL exception.  Run with:

    python3 -m examples.rule_trial
"""

import json
import sqlite3
import tempfile
from pathlib import Path

from pagefold.attempts import get_attempt, rehearse
from pagefold.decisions import resolve
from pagefold.images import register
from pagefold.plans import create_plan, read_plan
from pagefold.rule_demo import RULES, scenario_image
from pagefold.rules import create_rule
from pagefold.store import Store
from pagefold.suggestions import trial_plan


HUMAN_CHOICES = {
    "delete-edit": ("right", "人采纳覆盖规则：保留右支修改，拒绝左支删除。"),
    "absent-null": ("right", "人确认右支是把备注改成 SQL NULL，而非删行；采纳试算。"),
    "weight": ("right", "同优先级规则意见冲突；人选择权重维护方右支。"),
    "null-weight": ("base", "人采纳例外规则：拒绝把权重清空为 SQL NULL。"),
}


def code_of(item):
    return item["key"][0][1]


def concise_trial(trial):
    rows = []
    for suggestion in trial["suggestions"]:
        rows.append({
            "conflict": code_of(suggestion),
            "scenario": suggestion["scenario"]["description"],
            "tags": suggestion["scenario"]["tags"],
            "presence": suggestion["presence"],
            "changed_columns": suggestion["changed_columns"],
            "status": suggestion["status"],
            "explanation": suggestion["explanation"],
            "recommended_choice": suggestion["recommended_choice"],
            "opinions_differ": suggestion["opinions_differ"],
            "matched_rules": [{
                "rule": hit["rule_id"],
                "name": hit["name"],
                "category": hit["category"],
                "priority": hit["priority"],
                "opinion": hit["choice"],
                "reasons": hit["reasons"],
            } for hit in suggestion["matched_rules"]],
        })
    return {"revision": trial["revision"], "summary": trial["summary"], "conflicts": rows}


def candidate_rows(candidate):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(candidate)
        return [dict(zip(("code", "title", "note", "weight", "status"), row))
                for row in db.execute("SELECT code,title,note,weight,status FROM habit_case ORDER BY code")]
    finally:
        db.close()


def run():
    with tempfile.TemporaryDirectory(prefix="pagefold-rules-") as root:
        store = Store(Path(root) / "state.sqlite")
        sources = {
            role: register(store, scenario_image(role), label)["id"]
            for role, label in (("base", "规则祖本"), ("left", "规则左支"), ("right", "规则右支"))
        }
        plan = create_plan(store, sources)
        plan_id = plan["id"]

        # These are ordinary newly-created rules, not hard-coded merge behavior.
        for rule in RULES:
            create_rule(store, rule)

        with store.connect() as db:
            before = read_plan(db, plan_id)
            trial = trial_plan(db, plan_id)
        trial_view = concise_trial(trial)

        # A trial must not write decisions or advance revision.
        with store.connect() as db:
            after_trial = read_plan(db, plan_id)
        assert after_trial["revision"] == 0
        assert after_trial["unresolved"] == 4

        by_code = {code_of(s): s for s in trial["suggestions"]}
        assert by_code["delete-edit"]["recommended_choice"] == "right"
        assert by_code["absent-null"]["recommended_choice"] == "right"
        assert by_code["weight"]["status"] == "contested"
        assert by_code["null-weight"]["status"] == "exception_recommendation"
        assert by_code["null-weight"]["recommended_choice"] == "base"

        writes = []
        revision = before["revision"]
        for code, (choice, human_reason) in HUMAN_CHOICES.items():
            item = next(item for item in before["items"] if code_of(item) == code)
            revision_before = revision
            result = resolve(store, plan_id, revision, item["id"], choice)
            writes.append({"conflict": code, "choice": choice, "human_reason": human_reason,
                           "revision_before": revision_before,
                           "revision_after": result["revision"],
                           "changed": result["revision"] != revision_before})
            revision = result["revision"]

        with store.connect() as db:
            after_decisions = read_plan(db, plan_id)
        assert after_decisions["revision"] == 4
        assert after_decisions["unresolved"] == 0

        rehearsal = rehearse(store, plan_id, revision)
        assert rehearsal["status"] == "passed", rehearsal
        with store.connect() as db:
            attempt = get_attempt(db, plan_id, revision)
            final_rows = candidate_rows(attempt["candidate"])

        rows_by_code = {row["code"]: row for row in final_rows}
        assert rows_by_code["delete-edit"]["status"] == "reviewed"
        assert rows_by_code["absent-null"]["note"] is None
        assert rows_by_code["weight"]["weight"] == 12.0
        assert rows_by_code["null-weight"]["weight"] == 10.0

        return {
            "plan_id": plan_id,
            "before_writes": {"revision": before["revision"], "unresolved": before["unresolved"],
                              "state": before["state"]},
            "dry_run": trial_view,
            "human_writes": writes,
            "after_writes": {"revision": after_decisions["revision"],
                             "unresolved": after_decisions["unresolved"],
                             "choices": {code_of(i): i["choice"] for i in after_decisions["items"]}},
            "rehearsal_result": {k: v for k, v in rehearsal.items() if k != "id"},
            "final_database_rows": final_rows,
            "checks": [
                "试算后版本仍为 0，未写入裁决",
                "左支删除/右支修改被识别并由高优先级规则覆盖广谱左支意见",
                "两支都写且仅右支改 weight 与删除/修改类别分离",
                "同优先级重叠规则产生 contested，不自动指定结论",
                "缺席（整行不存在）与 SQL NULL（行存在、单元格为 n）分别给出证据",
                "四次人工裁决使版本 0 -> 4",
                "当前版重演通过，最终 SQLite 行符合人工选择",
            ],
        }


def main():
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
