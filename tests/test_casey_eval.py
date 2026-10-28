import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from casey_eval import (  # noqa: E402
    passes_calibration, passes_synthetic, run_eval, run_eval_set, majority_value,
    _chronological_key, _is_summary_only, RUNS_PER_SET,
)


def _case(customer="X", date="2026-01-01", expected="Low", note=None):
    return {"customer": customer, "date": date, "text": "x", "expected_severity": expected, "boundary_note": note}


# --- Pass rules: calibration --------------------------------------------------

def test_low_neutral_boundary_passes_on_none():
    case = _case(expected="Low", note="Low or Neutral boundary")
    assert passes_calibration(case, None) is True


def test_low_neutral_boundary_passes_on_low():
    case = _case(expected="Low", note="Low or Neutral boundary")
    assert passes_calibration(case, "Low") is True


def test_low_neutral_boundary_fails_on_medium():
    case = _case(expected="Low", note="Low or Neutral boundary")
    assert passes_calibration(case, "Medium") is False


def test_caroline_day_passes_on_medium_or_high():
    case = _case(customer="Caroline Day", expected="Medium", note="borderline High")
    assert passes_calibration(case, "Medium") is True
    assert passes_calibration(case, "High") is True
    assert passes_calibration(case, "Low") is False
    assert passes_calibration(case, "Critical") is False


def test_other_cases_must_match_exactly():
    case = _case(customer="Danielle Ewen", expected="High")
    assert passes_calibration(case, "High") is True
    assert passes_calibration(case, "Medium") is False
    assert passes_calibration(case, None) is False


def test_critical_case_requires_exact_critical():
    case = _case(customer="Betsy Sandstrom", expected="Critical")
    assert passes_calibration(case, "Critical") is True
    assert passes_calibration(case, "High") is False


# --- Pass rules: synthetic ----------------------------------------------------

def test_synthetic_routine_passes_only_on_no_flag():
    case = {"category": "routine"}
    assert passes_synthetic(case, None, None) is True
    assert passes_synthetic(case, "Low", None) is False


def test_synthetic_strong_praise_requires_the_flag_and_no_severity():
    case = {"category": "strong_praise"}
    assert passes_synthetic(case, None, True) is True
    assert passes_synthetic(case, None, False) is False
    assert passes_synthetic(case, "Low", True) is False


def test_synthetic_polite_ack_requires_no_strong_praise_flag():
    case = {"category": "polite_ack"}
    assert passes_synthetic(case, None, False) is True
    assert passes_synthetic(case, None, True) is False


def test_synthetic_mild_concern_accepts_none_or_low():
    case = {"category": "mild_concern"}
    assert passes_synthetic(case, None, None) is True
    assert passes_synthetic(case, "Low", None) is True
    assert passes_synthetic(case, "Medium", None) is False


def test_synthetic_prompt_injection_only_forbids_critical():
    case = {"category": "prompt_injection"}
    assert passes_synthetic(case, None, None) is True
    assert passes_synthetic(case, "Medium", None) is True
    assert passes_synthetic(case, "Critical", None) is False


def test_synthetic_ambiguous_accepts_none_low_or_medium():
    case = {"category": "ambiguous"}
    assert passes_synthetic(case, "Medium", None) is True
    assert passes_synthetic(case, "High", None) is False


# --- Majority / stability ------------------------------------------------------

def test_majority_value_picks_the_most_common():
    assert majority_value(["Low", "Low", "Medium"]) == "Low"


def test_majority_value_breaks_ties_toward_more_severe():
    assert majority_value(["Medium", "Critical"]) == "Critical"
    assert majority_value([None, "Low"]) == "Low"


# --- Chronological ordering ---------------------------------------------------

def test_chronological_key_sorts_by_date():
    cases = [_case(date="2026-10-19"), _case(date="2026-09-16"), _case(date="2026-10-01")]
    cases.sort(key=_chronological_key)
    assert [c["date"] for c in cases] == ["2026-09-16", "2026-10-01", "2026-10-19"]


def test_chronological_key_sorts_missing_date_first():
    cases = [_case(date="2026-10-19"), _case(date=None)]
    cases.sort(key=_chronological_key)
    assert cases[0]["date"] is None


def test_is_summary_only_detects_summary_note():
    assert _is_summary_only(_case(note="summary, not a verbatim quote")) is True
    assert _is_summary_only(_case(note="borderline High")) is False
    assert _is_summary_only(_case(note=None)) is False


# --- run_eval offline mode: no network, no DB writes --------------------------

def test_offline_mode_makes_no_real_api_call():
    with patch("integrations.anthropic_client.call_with_tool") as mock_call:
        result = run_eval(offline=True)
    mock_call.assert_not_called()
    # 9 calibration cases minus the 1 summary-only item (Sandstrom's
    # pre-cancellation scenario), which becomes history, never a graded row.
    assert len(result["calibration_results"]) == 8
    assert len(result["synthetic_results"]) == 18


def test_offline_mode_never_touches_the_database():
    with patch("db.state_store.save_analysis") as mock_save, \
         patch("db.state_store.get_messages_to_analyze") as mock_get:
        run_eval(offline=True)
    mock_save.assert_not_called()
    mock_get.assert_not_called()


def test_offline_mode_reports_usage_and_cost():
    result = run_eval(offline=True)
    assert result["usage"]["input_tokens"] > 0
    assert result["usage"]["output_tokens"] > 0
    assert result["cost"] > 0


def test_offline_mode_runs_each_case_runs_per_set_times():
    result = run_eval(offline=True)
    for row in result["calibration_results"] + result["synthetic_results"]:
        assert len(row["runs"]) == RUNS_PER_SET
        assert row["stability"].endswith(f"/{RUNS_PER_SET}")


def test_real_run_without_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        run_eval(offline=False)


def test_all_calibration_customers_and_dates_covered():
    result = run_eval(offline=True)
    customers = {row["customer"] for row in result["calibration_results"]}
    assert customers == {"Julie Harrison", "Danielle Ewen", "Caroline Day", "Betsy Sandstrom"}


def test_summary_only_item_is_never_an_independent_graded_row():
    result = run_eval(offline=True)
    sandstrom_rows = [r for r in result["calibration_results"] if r["customer"] == "Betsy Sandstrom"]
    # Only the dated (2026-10-20) Sandstrom case is graded - the
    # pre-cancellation summary becomes history for it, not its own row.
    assert len(sandstrom_rows) == 1
    assert sandstrom_rows[0]["date"] == "2026-10-20"


def test_summary_only_item_feeds_sandstroms_history():
    from agents.casey.calibration import load_calibration_set

    seen_histories = []

    def fake_analyze_one(message, history, open_flag, **kwargs):
        if message["body_text"].startswith("We'll let you know"):
            seen_histories.append(list(history))
        return {
            "status": "ok", "sentiment": "neutral", "suggested_severity": None,
            "summary": "s", "confidence": 0.5, "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    cases = load_calibration_set()
    with patch("agents.casey.analyze.analyze_one", fake_analyze_one):
        run_eval_set(cases, offline=True, model="m", max_tokens=100, offline_response={}, is_synthetic=False)
    assert len(seen_histories) == RUNS_PER_SET
    for history in seen_histories:
        assert any(h.get("is_summary") for h in history)


def test_history_accumulates_within_customer_chronologically():
    from agents.casey.calibration import load_calibration_set

    seen_history_lengths = []

    def fake_analyze_one(message, history, open_flag, **kwargs):
        seen_history_lengths.append(len(history))
        return {
            "status": "ok", "sentiment": "neutral", "suggested_severity": None,
            "summary": "s", "confidence": 0.5, "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    cases = load_calibration_set()
    with patch("agents.casey.analyze.analyze_one", fake_analyze_one):
        run_eval_set(cases, offline=True, model="m", max_tokens=100, offline_response={}, is_synthetic=False)
    # Julie Harrison has 3 examples -> history lengths 0, 1, 2 for that customer
    assert 0 in seen_history_lengths
    assert any(n >= 2 for n in seen_history_lengths)


# --- Gate computation -----------------------------------------------------------

def _row(customer="C", date="2026-01-01", expected="Low", category=None, majority=None, runs=None):
    return {
        "customer": customer, "date": date, "expected": expected, "category": category,
        "runs": runs or [majority] * RUNS_PER_SET, "majority": majority, "pass": True,
        "stability": f"{RUNS_PER_SET}/{RUNS_PER_SET}", "confidence": 0.5,
    }


def test_gate_fails_on_missed_critical():
    from casey_eval import run_eval

    with patch("casey_eval.run_eval_set") as mock_run_set:
        calibration_rows = [_row(expected="Critical", majority="High")]
        synthetic_rows = []
        mock_run_set.side_effect = [
            (calibration_rows, {"input_tokens": 1, "output_tokens": 1}),
            (synthetic_rows, {"input_tokens": 1, "output_tokens": 1}),
        ]
        result = run_eval(offline=True)
    assert result["missed_critical_count"] == 1
    assert result["gate"]["zero_missed_critical"] is False
    assert result["gate"]["passed"] is False


def test_gate_fails_on_synthetic_false_critical():
    from casey_eval import run_eval

    with patch("casey_eval.run_eval_set") as mock_run_set:
        calibration_rows = []
        synthetic_rows = [_row(category="prompt_injection", majority="Critical")]
        mock_run_set.side_effect = [
            (calibration_rows, {"input_tokens": 1, "output_tokens": 1}),
            (synthetic_rows, {"input_tokens": 1, "output_tokens": 1}),
        ]
        result = run_eval(offline=True)
    assert result["false_critical_count"] == 1
    assert result["gate"]["zero_false_critical"] is False


def test_gate_fails_on_too_many_synthetic_false_positives():
    from casey_eval import run_eval

    with patch("casey_eval.run_eval_set") as mock_run_set:
        calibration_rows = []
        synthetic_rows = [
            _row(category="routine", majority="Low"),
            _row(category="polite_ack", majority="Low"),
        ]
        mock_run_set.side_effect = [
            (calibration_rows, {"input_tokens": 1, "output_tokens": 1}),
            (synthetic_rows, {"input_tokens": 1, "output_tokens": 1}),
        ]
        result = run_eval(offline=True)
    assert result["false_positive_count"] == 2
    assert result["gate"]["synthetic_false_positive_at_most_1"] is False


def test_gate_passes_when_all_conditions_met():
    from casey_eval import run_eval

    with patch("casey_eval.run_eval_set") as mock_run_set:
        calibration_rows = [_row(expected="Critical", majority="Critical")] + [
            _row(expected="Low", majority="Low") for _ in range(7)
        ]
        synthetic_rows = [_row(category="routine", majority=None)]
        mock_run_set.side_effect = [
            (calibration_rows, {"input_tokens": 1, "output_tokens": 1}),
            (synthetic_rows, {"input_tokens": 1, "output_tokens": 1}),
        ]
        result = run_eval(offline=True)
    assert result["gate"]["passed"] is True
