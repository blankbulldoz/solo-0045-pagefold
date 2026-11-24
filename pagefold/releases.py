import json

from .attempts import get_attempt
from .codec import canonical, digest, fingerprint
from .errors import Problem
from .images import get_image
from .plans import expect_open, expect_revision, get_plan, plan_content

# Fields the operator explicitly confirms against the rendered evidence.
CONFIRMATION_FIELDS = ("plan", "revision", "candidate_digest", "sources")


def get_release(db, plan_id, body=False):
    columns = "certificate,candidate" if body else "certificate"
    row = db.execute(f"SELECT {columns} FROM releases WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise Problem("unpublished", "该计划还没有封存成果。", 404)
    return row


def source_roles(db, sources):
    roles = []
    for role in ("base", "left", "right"):
        image = get_image(db, sources[role])
        roles.append({"role": role, "id": sources[role], "label": image["label"]})
    return roles


def build_evidence(db, plan):
    """The exact, server-side object that a seal confirmation must bind to.

    Returns None when the current revision has no passed rehearsal candidate.
    """
    revision = plan["revision"]
    attempt = get_attempt(db, plan["id"], revision)
    if attempt is None or attempt["candidate"] is None:
        return None
    result = json.loads(attempt["result"])
    candidate_digest = result.get("candidate_digest")
    if digest(attempt["candidate"]) != candidate_digest:
        raise Problem("candidate_digest", "候选摘要不一致，无法封存。", 409)
    items, decisions = plan_content(plan)
    sources = {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]}
    signature = json.loads(get_image(db, plan["base"])["catalog"])["signature"]
    return {"plan": plan["id"], "revision": revision, "attempt_revision": revision,
            "candidate_digest": candidate_digest, "candidate_size": result["size"],
            "sources": sources, "source_roles": source_roles(db, sources),
            "schema": signature, "plan_digest": fingerprint(items),
            "decision_digest": fingerprint(decisions)}


def binding(evidence):
    """The subset of evidence the client echoes to confirm what it saw."""
    return {field: evidence[field] for field in CONFIRMATION_FIELDS}


def complete_confirmation(confirmation):
    """A usable evidence binding: plan, numeric revision and candidate digest.

    Sources are echoed by the rendered form/canonical confirmation; an API
    caller may omit them because the plan row immutably fixes the role sources.
    When supplied, they must still match exactly.
    """
    return (isinstance(confirmation, dict)
            and isinstance(confirmation.get("plan"), str)
            and isinstance(confirmation.get("revision"), int)
            and isinstance(confirmation.get("candidate_digest"), str))


def latest_attempt_summary(db, plan_id):
    row = db.execute(
        "SELECT revision,result FROM attempts WHERE plan_id=? ORDER BY revision DESC LIMIT 1",
        (plan_id,)).fetchone()
    if row is None:
        return None
    result = json.loads(row["result"])
    return {"revision": row["revision"], "status": result.get("status"),
            "candidate_digest": result.get("candidate_digest")}


def comparison_object(db, plan):
    """Current evidence for a plan, usable as the 'new object' in a compare."""
    evidence = build_evidence(db, plan)
    if evidence is not None:
        data = dict(evidence)
        data["sealable"] = True
        return data
    sources = {"base": plan["base"], "left": plan["left_id"], "right": plan["right_id"]}
    return {"plan": plan["id"], "revision": plan["revision"], "state": plan["state"],
            "sealable": False, "candidate_digest": None, "candidate_size": None,
            "sources": sources, "source_roles": source_roles(db, sources),
            "latest_attempt": latest_attempt_summary(db, plan["id"])}


def other_plan(db, plan_id):
    row = db.execute("SELECT id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        return None
    plan = get_plan(db, plan_id)
    current = comparison_object(db, plan)
    current.update({"id": plan_id, "href": f"/plans/{plan_id}"})
    return current


def seal_preview(db, plan_id):
    plan = get_plan(db, plan_id)
    evidence = build_evidence(db, plan)
    released = db.execute("SELECT 1 FROM releases WHERE plan_id=?", (plan_id,)).fetchone()
    preview = {"plan": plan_id, "state": plan["state"], "revision": plan["revision"],
               "sealed": bool(released), "sealable": evidence is not None and not released}
    if evidence is not None:
        preview.update(evidence)
        preview["confirmation"] = binding(evidence)
    else:
        preview["confirmation"] = None
        preview["current"] = comparison_object(db, plan)
    if released:
        preview["certificate"] = json.loads(get_release(db, plan_id)["certificate"])
    return preview


def _claimed_plan(confirmation):
    return confirmation.get("plan") if isinstance(confirmation, dict) else None


def _replay_certificate(db, plan, confirmation):
    """Recognise a retried seal whose response was lost.

    An exact replay of the successful confirmation returns the same certificate.
    A different confirmation against an already frozen plan is a conflict, never
    a silent re-seal.
    """
    row = db.execute("SELECT certificate FROM releases WHERE plan_id=?", (plan["id"],)).fetchone()
    if row is None:
        return None
    certificate = json.loads(row["certificate"])
    if not complete_confirmation(confirmation):
        # Plain retry / legacy caller: re-identify the same frozen release.
        return certificate
    expected = {"plan": certificate["plan"], "revision": certificate["revision"],
                "candidate_digest": certificate["candidate_digest"],
                "sources": certificate["sources"]}
    core_match = all(confirmation.get(field) == expected[field]
                     for field in ("plan", "revision", "candidate_digest"))
    sources = confirmation.get("sources")
    sources_match = sources is None or sources == expected["sources"]
    if core_match and sources_match:
        return certificate
    submitted = {field: confirmation.get(field) for field in CONFIRMATION_FIELDS}
    raise Problem("seal_conflict", "该计划已经封存，但提交的确认与封存记录不一致。", 409,
                  confirmed=submitted, current=_certificate_comparison(certificate))


def _certificate_comparison(certificate):
    return {"plan": certificate["plan"], "revision": certificate["revision"], "sealable": True,
            "sealed": True, "candidate_digest": certificate["candidate_digest"],
            "candidate_size": certificate.get("candidate_size"),
            "sources": certificate["sources"]}


def _check_plan_binding(db, plan_id, confirmation):
    claimed = _claimed_plan(confirmation)
    if claimed is not None and claimed != plan_id:
        raise Problem(
            "confirmation_plan_mismatch",
            "这份封存确认属于另一个计划；即使版序和候选摘要相同，也不能用于当前计划。",
            409, plan=plan_id, confirmation_plan=claimed,
            current=comparison_object(db, get_plan(db, plan_id)),
            other_plan=other_plan(db, claimed))


def _check_freshness(db, plan, confirmation):
    """Reject a confirmation that no longer names the current evidence."""
    if confirmation is None:
        return
    current = comparison_object(db, plan)
    confirmed = {field: confirmation.get(field) for field in CONFIRMATION_FIELDS}
    revision_changed = confirmation.get("revision") != plan["revision"]
    digest_changed = confirmation.get("candidate_digest") != current.get("candidate_digest")
    if revision_changed or digest_changed:
        reason = "revision_changed" if revision_changed else "candidate_changed"
        raise Problem(
            "stale_confirmation",
            "封存确认已过期：你确认的是旧版重演证据，下方给出了新的比较对象，请重新核对后确认。",
            409, reason=reason, confirmed=confirmed, current=current)
    if confirmation.get("sources") is not None and confirmation.get("sources") != current["sources"]:
        raise Problem(
            "confirmation_mismatch",
            "确认中的三方来源与计划绑定的来源角色不一致。", 409,
            confirmed=confirmed, current=current)


def seal(store, plan_id, revision, acknowledged=False, confirmation=None):
    with store.connect(write=True) as db:
        plan = get_plan(db, plan_id)
        if isinstance(confirmation, dict):
            # The URL is authoritative for the plan; an explicit different
            # plan is what reveals a confirmation copied from another tab.
            confirmation.setdefault("plan", plan_id)
        # Cross-plan confirmations are diagnosed before any version comparison:
        # two tabs can share the same revision number and candidate digest.
        _check_plan_binding(db, plan_id, confirmation)
        # An exact retry after a lost response re-identifies the same release.
        replayed = _replay_certificate(db, plan, confirmation)
        if replayed is not None:
            return replayed
        complete = complete_confirmation(confirmation)
        if complete:
            # A complete confirmation carries the evidence the operator saw;
            # judge staleness against it before falling back to revision checks.
            _check_freshness(db, plan, confirmation)
        expect_revision(plan, revision)
        expect_open(plan)
        evidence = build_evidence(db, plan)
        if evidence is None:
            raise Problem("rehearsal_required", "当前版本须先通过重演。", 409)
        if confirmation is None and acknowledged:
            # A bare explicit flag confirms the evidence currently rendered;
            # the page form and canonical confirmation still bind every field.
            confirmation = binding(evidence)
            complete = True
        if not complete or not acknowledged:
            raise Problem(
                "confirmation_required",
                "封存前请清楚核对将要冻结的候选摘要与三方来源，并明确确认。",
                409, evidence=evidence, confirmation=binding(evidence))
        certificate = {"format": "pagefold-certificate-v1", "plan": plan_id, "revision": revision,
                       "sources": evidence["sources"], "schema": evidence["schema"],
                       "plan_digest": evidence["plan_digest"],
                       "decision_digest": evidence["decision_digest"],
                       "candidate_digest": evidence["candidate_digest"],
                       "candidate_size": evidence["candidate_size"]}
        db.execute("INSERT INTO releases VALUES(?,?,?)",
                   (plan_id, canonical(certificate),
                    get_attempt(db, plan_id, revision)["candidate"]))
        db.execute("UPDATE plans SET state='sealed' WHERE id=?", (plan_id,))
        return certificate
