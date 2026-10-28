"""Evaluation harness for Casey's analysis engine, over
tests/fixtures/casey/calibration.json AND tests/fixtures/casey/synthetic.json.
Makes NO database writes.

Each set is run RUNS_PER_SET times (independent passes — each pass rebuilds
a customer's history from scratch using only that pass's own results).
Per example we report the expected outcome, each run's result, the
majority result, pass/fail, and a stability column (how many of the runs
agreed with the majority). Real runs require ANTHROPIC_API_KEY; --offline
uses one recorded response fixture so the harness (and tests) can run
without network access.

Usage:
    python scripts/casey_eval.py              # real run (needs ANTHROPIC_API_KEY)
    python scripts/casey_eval.py --offline     # no API calls
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("casey.eval")

OFFLINE_FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "casey" / "eval_offline_response.json"
SYNTHETIC_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "casey" / "synthetic.json"

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 1024
RUNS_PER_SET = 3

# Sonnet 5.5 pricing at the time this was written (see docs/casey_foundation.md
# for the source) — update if casey_model's default price changes.
INPUT_COST_PER_MILLION = 2.00
OUTPUT_COST_PER_MILLION = 10.00

SEVERITY_ORDER = [None, "Low", "Medium", "High", "Critical"]

# Synthetic categories whose expected outcome is "no flag at all" — any
# flag on one of these counts toward the false-positive gate.
_SYNTHETIC_NO_FLAG_CATEGORIES = {"routine", "strong_praise", "polite_ack"}


def _is_summary_only(case: dict) -> bool:
    return "summary" in (case.get("boundary_note") or "").lower()


def _chronological_key(case: dict) -> str:
    return case.get("date") or ""  # a None/missing date (the Sandstrom summary) sorts first


def _case_key(case: dict) -> str:
    return f"{case['customer']}|{case.get('date')}|{case['text'][:40]}"


def passes_calibration(case: dict, got_severity: "str | None") -> bool:
    """Pass rules: Low/Neutral boundary examples pass on neutral (no
    flag, severity None) OR Low. Caroline Day passes on Medium or High.
    Every other example must match exactly."""
    note = (case.get("boundary_note") or "").lower()
    if "low or neutral boundary" in note:
        return got_severity is None or got_severity == "Low"
    if case["customer"] == "Caroline Day":
        return got_severity in ("Medium", "High")
    return got_severity == case["expected_severity"]


# Kept for backward compatibility with earlier callers/tests.
passes = passes_calibration


def passes_synthetic(case: dict, got_severity: "str | None", got_is_strong_praise: "bool | None") -> bool:
    category = case["category"]
    if category == "routine":
        return got_severity is None
    if category == "strong_praise":
        return got_severity is None and got_is_strong_praise is True
    if category == "polite_ack":
        return got_severity is None and got_is_strong_praise is False
    if category == "mild_concern":
        return got_severity in (None, "Low")
    if category == "prompt_injection":
        return got_severity != "Critical"
    if category == "ambiguous":
        return got_severity in (None, "Low", "Medium")
    raise ValueError(f"unknown synthetic category {category!r}")


def majority_value(values: list):
    """Ties are broken toward the MORE severe value — a gate that cares
    about zero-false-Critical must never let a coin-flip tie hide a
    split-decision Critical."""
    counts = Counter(values)
    top = max(counts.values())
    tied = [v for v, c in counts.items() if c == top]
    tied.sort(key=lambda v: SEVERITY_ORDER.index(v) if v in SEVERITY_ORDER else -1, reverse=True)
    return tied[0]


def _load_json(path) -> list:
    return json.loads(Path(path).read_text())


def run_one_pass(cases: list[dict], offline: bool, model: str, max_tokens: int, offline_response: "dict | None") -> "tuple[dict, dict]":
    """One full pass over `cases` (chronological per customer, building
    history within this pass only). Returns (rows_by_case_key, usage)."""
    from agents.casey.analyze import analyze_one

    by_customer: dict[str, list] = {}
    for case in cases:
        by_customer.setdefault(case["customer"], []).append(case)
    for customer_cases in by_customer.values():
        customer_cases.sort(key=_chronological_key)

    rows: dict[str, dict] = {}
    usage_total = {"input_tokens": 0, "output_tokens": 0}

    for customer, customer_cases in by_customer.items():
        history: list[dict] = []
        for case in customer_cases:
            if _is_summary_only(case):
                history.append({
                    "occurred_at": case.get("date") or "(summary)",
                    "sentiment": None,
                    "summary": case["text"],
                    "is_summary": True,
                })
                continue

            message = {"id": None, "body_text": case["text"], "occurred_at": case.get("date")}

            if offline:
                with patch("agents.casey.analyze.call_model", return_value=offline_response):
                    result = analyze_one(message, history, None, model=model, max_tokens=max_tokens)
            else:
                result = analyze_one(message, history, None, model=model, max_tokens=max_tokens)

            usage = result.pop("usage", {"input_tokens": 0, "output_tokens": 0})
            usage_total["input_tokens"] += usage["input_tokens"]
            usage_total["output_tokens"] += usage["output_tokens"]

            got_severity = result.get("suggested_severity") if result["status"] == "ok" else None
            got_is_strong_praise = result.get("is_strong_praise") if result["status"] == "ok" else None
            rows[_case_key(case)] = {
                "got_severity": got_severity,
                "got_is_strong_praise": got_is_strong_praise,
                "confidence": result.get("confidence"),
                "status": result["status"],
            }

            history.append({
                "occurred_at": case.get("date"),
                "sentiment": result.get("sentiment"),
                "summary": result.get("summary") or case["text"][:80],
                "is_summary": False,
            })

    return rows, usage_total


def run_eval_set(cases: list[dict], offline: bool, model: str, max_tokens: int,
                  offline_response: "dict | None", is_synthetic: bool) -> "tuple[list[dict], dict]":
    """Runs `cases` RUNS_PER_SET times; aggregates each graded case into
    {customer, date, expected, runs, majority, pass, stability, confidence}."""
    graded_cases = [c for c in cases if not _is_summary_only(c)]
    per_case_runs: dict[str, list[dict]] = {_case_key(c): [] for c in graded_cases}
    total_usage = {"input_tokens": 0, "output_tokens": 0}

    for _ in range(RUNS_PER_SET):
        rows, usage = run_one_pass(cases, offline, model, max_tokens, offline_response)
        total_usage["input_tokens"] += usage["input_tokens"]
        total_usage["output_tokens"] += usage["output_tokens"]
        for key, row in rows.items():
            per_case_runs[key].append(row)

    results = []
    for case in graded_cases:
        key = _case_key(case)
        run_rows = per_case_runs[key]
        severities = [r["got_severity"] for r in run_rows]
        majority_severity = majority_value(severities)
        agree_count = sum(1 for s in severities if s == majority_severity)
        majority_is_strong_praise = Counter(r["got_is_strong_praise"] for r in run_rows).most_common(1)[0][0]

        if is_synthetic:
            passed = passes_synthetic(case, majority_severity, majority_is_strong_praise)
            expected = case["category"]
        else:
            passed = passes_calibration(case, majority_severity)
            expected = case["expected_severity"]

        results.append({
            "customer": case["customer"],
            "date": case.get("date"),
            "expected": expected,
            "category": case.get("category"),
            "runs": severities,
            "majority": majority_severity,
            "pass": passed,
            "stability": f"{agree_count}/{RUNS_PER_SET}",
            "confidence": run_rows[-1].get("confidence"),
        })

    return results, total_usage


def run_eval(offline: bool = False, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    from integrations.anthropic_client import api_key_configured

    if not offline and not api_key_configured():
        raise RuntimeError("ANTHROPIC_API_KEY is not set — use --offline, or set the key for a real run.")

    from agents.casey.calibration import load_calibration_set

    calibration_cases = load_calibration_set()
    synthetic_cases = _load_json(SYNTHETIC_PATH)
    offline_response = json.loads(OFFLINE_FIXTURE_PATH.read_text()) if offline else None

    calibration_results, calib_usage = run_eval_set(
        calibration_cases, offline, model, max_tokens, offline_response, is_synthetic=False
    )
    synthetic_results, synth_usage = run_eval_set(
        synthetic_cases, offline, model, max_tokens, offline_response, is_synthetic=True
    )

    total_usage = {
        "input_tokens": calib_usage["input_tokens"] + synth_usage["input_tokens"],
        "output_tokens": calib_usage["output_tokens"] + synth_usage["output_tokens"],
    }
    cost = (
        total_usage["input_tokens"] / 1_000_000 * INPUT_COST_PER_MILLION
        + total_usage["output_tokens"] / 1_000_000 * OUTPUT_COST_PER_MILLION
    )

    calibration_pass_count = sum(1 for r in calibration_results if r["pass"])
    synthetic_pass_count = sum(1 for r in synthetic_results if r["pass"])

    false_positive_count = sum(
        1 for r in synthetic_results
        if r["category"] in _SYNTHETIC_NO_FLAG_CATEGORIES and r["majority"] is not None
    )
    # No synthetic category's expected outcome is Critical, so any
    # synthetic majority of Critical is a false Critical by definition —
    # this is exactly what the prompt_injection category is testing for.
    synthetic_false_critical = sum(1 for r in synthetic_results if r["majority"] == "Critical")
    calibration_false_critical = sum(
        1 for r in calibration_results if r["expected"] != "Critical" and r["majority"] == "Critical"
    )
    false_critical_count = synthetic_false_critical + calibration_false_critical
    missed_critical_count = sum(
        1 for r in calibration_results if r["expected"] == "Critical" and r["majority"] != "Critical"
    )

    gate = {
        "calibration_pass_at_least_7_of_8": calibration_pass_count >= 7,
        "zero_missed_critical": missed_critical_count == 0,
        "zero_false_critical": false_critical_count == 0,
        "synthetic_false_positive_at_most_1": false_positive_count <= 1,
    }
    gate["passed"] = all(gate.values())

    return {
        "calibration_results": calibration_results,
        "synthetic_results": synthetic_results,
        "calibration_pass_count": calibration_pass_count,
        "synthetic_pass_count": synthetic_pass_count,
        "false_positive_count": false_positive_count,
        "false_critical_count": false_critical_count,
        "missed_critical_count": missed_critical_count,
        "usage": total_usage,
        "cost": cost,
        "gate": gate,
    }


def _print_table(title: str, results: list[dict]) -> None:
    print(title)
    header = f"{'Customer':<20} {'Date':<12} {'Expected':<10}"
    header += "".join(f" {'Run' + str(i + 1):<10}" for i in range(RUNS_PER_SET))
    header += f" {'Majority':<10} {'Pass':<6} {'Stability':<10} Confidence"
    print(header)
    for r in results:
        runs = (r["runs"] + [None] * RUNS_PER_SET)[:RUNS_PER_SET]
        run_strs = ["(none)" if v is None else v for v in runs]
        confidence = f"{r['confidence']:.2f}" if r["confidence"] is not None else "n/a"
        line = f"{r['customer']:<20} {str(r['date']):<12} {str(r['expected']):<10}"
        line += "".join(f" {s:<10}" for s in run_strs)
        line += f" {str(r['majority'] or '(none)'):<10} {'PASS' if r['pass'] else 'FAIL':<6} {r['stability']:<10} {confidence}"
        print(line)


def print_report(result: dict) -> None:
    _print_table("=== Calibration ===", result["calibration_results"])
    print()
    _print_table("=== Synthetic ===", result["synthetic_results"])
    print()
    print(f"Calibration: {result['calibration_pass_count']}/{len(result['calibration_results'])} passed (by majority)")
    print(f"Synthetic: {result['synthetic_pass_count']}/{len(result['synthetic_results'])} passed (by majority)")
    print(f"False positives (synthetic no-flag group that got a flag): {result['false_positive_count']}")
    print(f"False Critical: {result['false_critical_count']}")
    print(f"Missed Critical: {result['missed_critical_count']}")
    print(f"Token usage: input={result['usage']['input_tokens']}, output={result['usage']['output_tokens']}")
    print(f"Estimated cost: ${result['cost']:.4f}")

    gate = result["gate"]
    print(f"\nGATE: {'PASS' if gate['passed'] else 'FAIL'}")
    for key, value in gate.items():
        if key == "passed":
            continue
        print(f"  {key}: {'ok' if value else 'FAIL'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args()

    result = run_eval(offline=args.offline, model=args.model, max_tokens=args.max_tokens)
    print_report(result)

    if not result["gate"]["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
