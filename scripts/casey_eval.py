"""Evaluation harness for Casey's analysis engine, over
tests/fixtures/casey/calibration.json. Makes NO database writes.

Runs each customer's examples in chronological order so earlier
messages become history for later ones for the same customer. Real runs
require ANTHROPIC_API_KEY; --offline uses one recorded response fixture
so the harness (and tests) can run without network access.

Usage:
    python scripts/casey_eval.py              # real run (needs ANTHROPIC_API_KEY)
    python scripts/casey_eval.py --offline     # no API calls
"""
import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("casey.eval")

OFFLINE_FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "casey" / "eval_offline_response.json"

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 1024

# Sonnet 5.5 pricing at the time this was written (see docs/casey_foundation.md
# for the source) — update if casey_model's default price changes.
INPUT_COST_PER_MILLION = 2.00
OUTPUT_COST_PER_MILLION = 10.00


def _is_summary_only(case: dict) -> bool:
    return "summary" in (case.get("boundary_note") or "").lower()


def _chronological_key(case: dict) -> str:
    return case.get("date") or ""  # a None/missing date (the Sandstrom summary) sorts first


def passes(case: dict, got_severity: "str | None") -> bool:
    """Pass rules: Low/Neutral boundary examples pass on neutral (no
    flag, severity None) OR Low. Caroline Day passes on Medium or High.
    Every other example must match exactly."""
    note = (case.get("boundary_note") or "").lower()
    if "low or neutral boundary" in note:
        return got_severity is None or got_severity == "Low"
    if case["customer"] == "Caroline Day":
        return got_severity in ("Medium", "High")
    return got_severity == case["expected_severity"]


def run_eval(offline: bool = False, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    from agents.casey.calibration import load_calibration_set
    from agents.casey.analyze import analyze_one
    from integrations.anthropic_client import api_key_configured

    if not offline and not api_key_configured():
        raise RuntimeError("ANTHROPIC_API_KEY is not set — use --offline, or set the key for a real run.")

    cases = load_calibration_set()

    by_customer: dict[str, list] = {}
    for case in cases:
        by_customer.setdefault(case["customer"], []).append(case)
    for customer_cases in by_customer.values():
        customer_cases.sort(key=_chronological_key)

    offline_response = json.loads(OFFLINE_FIXTURE_PATH.read_text()) if offline else None

    rows = []
    total_usage = {"input_tokens": 0, "output_tokens": 0}

    for customer, customer_cases in by_customer.items():
        history: list[dict] = []
        for case in customer_cases:
            if _is_summary_only(case):
                # Not a real customer message to analyze — a scenario
                # summary (e.g. Sandstrom's pre-cancellation history) that
                # becomes history context for this customer's later
                # messages. Never independently graded as its own row.
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
            total_usage["input_tokens"] += usage["input_tokens"]
            total_usage["output_tokens"] += usage["output_tokens"]

            got_severity = result.get("suggested_severity") if result["status"] == "ok" else None
            row = {
                "customer": customer,
                "date": case.get("date"),
                "expected": case["expected_severity"],
                "got": got_severity if got_severity is not None else "(none)",
                "pass": passes(case, got_severity),
                "confidence": result.get("confidence"),
                "status": result["status"],
            }
            rows.append(row)

            history.append({
                "occurred_at": case.get("date"),
                "sentiment": result.get("sentiment"),
                "summary": result.get("summary") or case["text"][:80],
                "is_summary": False,
            })

    cost = (
        total_usage["input_tokens"] / 1_000_000 * INPUT_COST_PER_MILLION
        + total_usage["output_tokens"] / 1_000_000 * OUTPUT_COST_PER_MILLION
    )
    return {"rows": rows, "usage": total_usage, "cost": cost}


def print_report(result: dict) -> None:
    rows = result["rows"]
    print(f"{'Customer':<18} {'Date':<12} {'Expected':<10} {'Got':<10} {'Pass':<6} Confidence")
    for row in rows:
        confidence = f"{row['confidence']:.2f}" if row["confidence"] is not None else "n/a"
        print(f"{row['customer']:<18} {row['date']:<12} {row['expected']:<10} {row['got']:<10} "
              f"{'PASS' if row['pass'] else 'FAIL':<6} {confidence}")

    passed = sum(1 for r in rows if r["pass"])
    print(f"\n{passed}/{len(rows)} passed")
    print(f"Token usage: input={result['usage']['input_tokens']}, output={result['usage']['output_tokens']}")
    print(f"Estimated cost: ${result['cost']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args()

    result = run_eval(offline=args.offline, model=args.model, max_tokens=args.max_tokens)
    print_report(result)

    failed = [r for r in result["rows"] if not r["pass"]]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
