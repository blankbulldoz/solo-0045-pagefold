import json

from .codec import canonical
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, plan_content
from .rehearsal import AncestorImage, RehearsalJob, perform_rehearsal
from .replay import amendments, rebuild

__all__ = [
    "StateAncestorSource", "get_attempt", "list_attempts", "load_rehearsal_job",
    "rehearse", "amendments", "rebuild",
]


def get_attempt(db, plan_id, revision):
    return db.execute("SELECT result,candidate FROM attempts WHERE plan_id=? AND revision=?",
                      (plan_id, revision)).fetchone()


def list_attempts(db, plan_id):
    get_plan(db, plan_id)
    return [json.loads(row[0]) for row in db.execute(
        "SELECT result FROM attempts WHERE plan_id=? ORDER BY revision DESC", (plan_id,))]


class StateAncestorSource:
    """Load the common ancestor from the existing ``images`` state table."""

    def __init__(self, db, image_id):
        self.db = db
        self.image_id = image_id

    def load_ancestor(self):
        row = get_image(self.db, self.image_id, body=True)
        return AncestorImage(body=row["body"], catalog=json.loads(row["catalog"]))


def load_rehearsal_job(db, plan):
    # Decode plan JSON once here.  Catalog JSON is decoded by the source adapter
    # only after unresolved selections have been rejected.
    items, decisions = plan_content(plan)
    return RehearsalJob(
        plan_id=plan["id"],
        revision=plan["revision"],
        items=items,
        decisions=decisions,
        ancestor_source=StateAncestorSource(db, plan["base"]),
    )


def rehearse(store, plan_id, revision):
    with store.connect(write=True) as db:
        # Eligibility and the idempotent read stay in this storage-backed
        # boundary so HTTP and future callers cannot supply their own verdict.
        plan = get_plan(db, plan_id)
        expect_revision(plan, revision)
        expect_open(plan)

        previous = get_attempt(db, plan_id, revision)
        if previous is not None:
            return json.loads(previous["result"])

        job = load_rehearsal_job(db, plan)
        outcome = perform_rehearsal(job)

        # Both passed and blocked replay outcomes are facts.  Unresolved
        # selections escape before this point and therefore write no attempt.
        db.execute(
            "INSERT INTO attempts VALUES(?,?,?,?)",
            (plan_id, revision, canonical(outcome.result), outcome.candidate),
        )
        return outcome.result
