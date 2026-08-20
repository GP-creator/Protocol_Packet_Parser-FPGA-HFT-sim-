"""Metric fragments.

Every stage of `ci/check.sh` runs in its own process -- pytest, one Verilator
build per suite per width, the linter. None of them can hand a number to the
next. So each writes a small JSON fragment into `build/metrics/`, and
`ci/collect_metrics.py` merges the pile into `metrics.json` at the end.

Fragments are keyed by filename, so a rerun of one suite replaces only its own
numbers. `ci/check.sh` clears the directory first, which is what makes
`metrics.json` describe *this* run rather than the union of every run since the
last clean.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DIR = Path(os.environ.get("WIRESPEC_METRICS_DIR", ROOT / "build" / "metrics"))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def write(name: str, payload: dict) -> Path:
    """Write one fragment. Never raises: a metrics failure must not fail a test."""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        path = DIR / f"{_safe(name)}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        return path
    except OSError:  # read-only tree, full disk -- not a test result
        return DIR / f"{_safe(name)}.json"


def load_all() -> dict[str, dict]:
    if not DIR.is_dir():
        return {}
    out = {}
    for p in sorted(DIR.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return out
