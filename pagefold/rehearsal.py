"""Source-independent rehearsal calculation.

The storage layer owns plan eligibility and idempotent attempt history.  This
module only calculates the outcome for an already-authorized rehearsal job, so
the same calculation can be fed by the state database, a file-backed source, or
another database input adapter.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from . import replay
from .codec import digest
from .errors import Problem


@dataclass(frozen=True)
class AncestorImage:
    """Decoded catalog and exact SQLite bytes for the common ancestor."""

    body: bytes
    catalog: Mapping[str, Any]

    def load_ancestor(self) -> "AncestorImage":
        return self


class AncestorSource(Protocol):
    """Port for adapters that can supply a common-ancestor image."""

    def load_ancestor(self) -> AncestorImage:
        ...


@dataclass(frozen=True)
class RehearsalJob:
    plan_id: str
    revision: int
    items: list[dict[str, Any]]
    decisions: Mapping[str, str]
    ancestor_source: AncestorSource


@dataclass(frozen=True)
class RehearsalOutcome:
    result: dict[str, Any]
    candidate: Optional[bytes]


def perform_rehearsal(job: RehearsalJob) -> RehearsalOutcome:
    """Run row selection and SQLite replay without touching attempt storage.

    Unresolved selections are raised before the ancestor source is asked for
    bytes.  Constraint and SQLite replay failures are normal blocked outcomes so
    the storage caller can persist the corresponding attempt fact.
    """

    operations = replay.amendments(job.items, job.decisions)
    result: dict[str, Any] = {
        "id": job.plan_id,
        "revision": job.revision,
        "changed": sum(map(len, operations.values())),
    }

    # Loading through the port happens only after selection policy passes.  A
    # state-backed adapter decodes its catalog here, exactly once per new attempt.
    ancestor = job.ancestor_source.load_ancestor()
    try:
        candidate = replay.rebuild(ancestor.body, ancestor.catalog, operations)
    except Problem as exc:
        result.update(status="blocked", problem=exc.as_dict())
        return RehearsalOutcome(result, None)
    except sqlite3.Error as exc:
        result.update(status="blocked", problem={"code": "sqlite_replay", "detail": str(exc)})
        return RehearsalOutcome(result, None)

    result.update(status="passed", candidate_digest=digest(candidate), size=len(candidate))
    return RehearsalOutcome(result, candidate)
