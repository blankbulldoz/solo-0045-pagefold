"""Compatibility alias for explicit stable entity-key planning."""

from .entities import *  # noqa: F401,F403
from .entities import (build_entities, normalize_specs, prepare_specs,
                       unresolved_entities)

__all__ = ["build_entities", "normalize_specs", "prepare_specs", "unresolved_entities"]
