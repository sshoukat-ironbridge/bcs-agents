"""Loads the calibration golden set (tests/fixtures/casey/calibration.json)
for later prompt/severity-classifier evaluation. No LLM calls here — this
just loads and validates the fixture's shape.
"""
import json
from pathlib import Path
from typing import Any

from agents.casey.taxonomy import SEVERITIES

_REQUIRED_KEYS = {"customer", "date", "text", "expected_severity", "boundary_note"}

DEFAULT_PATH = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "casey" / "calibration.json"


def load_calibration_set(path: "Path | str | None" = None) -> list[dict[str, Any]]:
    path = Path(path) if path else DEFAULT_PATH
    with open(path) as f:
        cases = json.load(f)

    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: expected a non-empty JSON list of calibration cases")

    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"{path}[{i}]: expected an object, got {type(case).__name__}")
        missing = _REQUIRED_KEYS - case.keys()
        if missing:
            raise ValueError(f"{path}[{i}]: missing required keys {sorted(missing)}")
        if not (case.get("text") or "").strip():
            raise ValueError(f"{path}[{i}]: 'text' must be non-empty")
        if case["expected_severity"] not in SEVERITIES:
            raise ValueError(
                f"{path}[{i}]: expected_severity {case['expected_severity']!r} is not in {SEVERITIES}"
            )

    return cases
