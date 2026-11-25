"""Compatibility-facing helpers for callers that name the feature "renames".

The implementation lives in :mod:`pagefold.entities`; this module exposes the
verb-oriented names used by some plan and test entry points.
"""

from .entities import (build_entities, normalize_specs, normalize_entity_decision,
                       prepare_specs)

__all__ = [
    "detect_renames", "identify_renames", "plan_renames", "rename_groups",
    "normalize_specs", "normalize_entity_decision",
]


def detect_renames(base, left, right, catalog, entity_keys):
    """Return entity/rename groups from three typed row snapshots."""
    specs = prepare_specs(catalog, entity_keys)
    snapshots = {"base": base, "left": left, "right": right}
    return build_entities(snapshots, catalog, specs)


identify_renames = detect_renames
plan_renames = detect_renames
rename_groups = build_entities
