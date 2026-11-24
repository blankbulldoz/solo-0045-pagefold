import json

from .codec import canonical
from .errors import Problem
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, names_of, plan_content, scope_report
from .scope import analyze
from .rows import read_image
from .sqlite_image import mounted


def change_scope(store, plan_id, revision, scope):
    with store.connect(write=True) as db:
        row = get_plan(db, plan_id)
        expect_revision(row, revision)
        expect_open(row)
        catalog = json.loads(get_image(db, row["base"])["catalog"])
        if scope is None:
            normalized, stored = names_of(catalog), None
        else:
            if not isinstance(scope, list) or not scope or not all(isinstance(n, str) for n in scope):
                raise Problem("scope", "范围须是非空的表名数组。", 400)
            names = names_of(catalog)
            if len(set(scope)) != len(scope) or any(name not in names for name in scope):
                raise Problem("scope", "范围含有重复或不存在的表名。", 400)
            normalized, stored = list(scope), canonical(list(scope))
        items, decisions = plan_content(row)
        base = get_image(db, row["base"], body=True)
        with mounted(base["body"]) as snapshot:
            base_snapshot = read_image(snapshot, catalog)
        report = analyze(catalog, base_snapshot, items, decisions, normalized, [])
        db.execute("UPDATE plans SET scope=?,amendments=?,revision=? WHERE id=?",
                   (stored, canonical(report["confirmed"]), revision + 1, plan_id))
        return {"id": plan_id, "revision": revision + 1, "scope": normalized,
                "pending_derived": report["pending_derived"], "blocked": report["blocked"]}


def confirm_amendments(store, plan_id, revision, confirmed):
    if not isinstance(confirmed, list) or not all(isinstance(entry, str) for entry in confirmed):
        raise Problem("amendments", "确认项须是派生条目的 id 数组。", 400)
    with store.connect(write=True) as db:
        row = get_plan(db, plan_id)
        expect_revision(row, revision)
        expect_open(row)
        items, decisions = plan_content(row)
        base = get_image(db, row["base"], body=True)
        report = scope_report(row, base)
        valid = {entry["id"] for entry in report["derived"]}
        unknown = [entry for entry in confirmed if entry not in valid]
        if unknown:
            raise Problem("amendments", "存在不属于当前范围分析的派生条目。", 400)
        kept = sorted(set(confirmed))
        db.execute("UPDATE plans SET amendments=?,revision=? WHERE id=?",
                   (canonical(kept), revision + 1, plan_id))
        return {"id": plan_id, "revision": revision + 1, "confirmed": kept}
