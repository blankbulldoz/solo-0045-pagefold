import json

from .plans import get_plan, plan_content
from .rules import list_rules, normalize_rule, trial_items


def catalog_columns(db, plan):
    base_row = db.execute("SELECT catalog FROM images WHERE id=?", (plan["base"],)).fetchone()
    catalog = json.loads(base_row["catalog"])
    return {table["name"]: table["columns"] for table in catalog["tables"]}


def trial_plan(db, plan_id, drafts=None, include_stored=True):
    """Run rules against a plan without writing decisions or advancing revision."""
    plan = get_plan(db, plan_id)
    items, decisions = plan_content(plan)
    columns_by_table = catalog_columns(db, plan)
    rules = list_rules(db) if include_stored else []
    for index, draft in enumerate(drafts or [], 1):
        rule = normalize_rule(draft, rule_id=f"draft-{index}")
        rule["persisted"] = False
        rules.append(rule)
    result = trial_items(items, columns_by_table, rules, decisions)
    result.update({
        "plan_id": plan_id,
        "revision": plan["revision"],
        "state": plan["state"],
        "stored_rule_count": len(list_rules(db)) if include_stored else 0,
        "draft_count": len(drafts or []),
    })
    return result
