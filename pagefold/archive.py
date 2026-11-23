from .plans import expect_revision, get_plan


def archive(store, plan_id, revision):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        if plan["state"] != "archived":
            db.execute("UPDATE plans SET state='archived' WHERE id=?", (plan_id,))
        return {"id": plan_id, "revision": revision, "state": "archived"}
