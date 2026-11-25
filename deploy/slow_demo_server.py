#!/usr/bin/env python3
"""Demo-only Pagefold entry point that slows accepted write transactions.

The rehearsal seam wraps ``pagefold.attempts.rebuild`` after ``rehearse`` has
executed ``BEGIN IMMEDIATE``. The seal seam wraps the ``get_image`` call inside
``seal``'s write transaction. Gate files containing seconds make both
deterministic. Production deployments should run ``python3 -m pagefold``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pagefold.attempts as attempts  # noqa: E402
import pagefold.releases as releases  # noqa: E402


def gate_delay(gate_env, marker_env):
    gate = os.environ.get(gate_env)
    if not gate or not Path(gate).exists():
        return 0.0
    try:
        delay = float(Path(gate).read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0.0
    marker = os.environ.get(marker_env)
    if marker and delay > 0:
        Path(marker).write_text(f"pid={os.getpid()} delay={delay}\n", encoding="utf-8")
    return delay


_original_rebuild = attempts.rebuild


def delayed_rebuild(*args, **kwargs):
    delay = gate_delay("PAGEFOLD_DEMO_REHEARSE_GATE", "PAGEFOLD_DEMO_LOCK_MARKER")
    if delay > 0:
        time.sleep(delay)
    return _original_rebuild(*args, **kwargs)


attempts.rebuild = delayed_rebuild

_original_get_image = releases.get_image


def delayed_get_image(*args, **kwargs):
    delay = gate_delay("PAGEFOLD_DEMO_SEAL_GATE", "PAGEFOLD_DEMO_SEAL_MARKER")
    if delay > 0:
        time.sleep(delay)
    return _original_get_image(*args, **kwargs)


releases.get_image = delayed_get_image

from pagefold.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
