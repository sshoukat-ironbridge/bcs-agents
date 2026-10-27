import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from casey_eval import passes, run_eval, _chronological_key, _is_summary_only  # noqa: E402


def _case(customer="X", date="2026-01-01", expected="Low", note=None):
    return {"customer": customer, "date": date, "text": "x", "expected_severity": expected, "boundary_note": note}


# --- Pass rules --------------------------------------------------------------

def test_low_neutral_boundary_passes_on_none():
    case = _case(expected="Low", note="Low or Neutral boundary")
    assert passes(case, None) is True


def test_low_neutral_boundary_passes_on_low():
    case = _case(expected="Low", note="Low or Neutral boundary")
    assert passes(case, "Low") is True


def test_low_neutral_boundary_fails_on_medium():
    case = _case(expected="Low", note="Low or Neutral boundary")
    assert passes(case, "Medium") is False


def test_caroline_day_passes_on_medium_or_high():
    case = _case(customer="Caroline Day", expected="Medium", note="borderline High")
    assert passes(case, "Medium") is True
    assert passes(case, "High") is True
    assert passes(case, "Low") is False
    assert passes(case, "Critical") is False


def test_other_cases_must_match_exactly():
    case = _case(customer="Danielle Ewen", expected="High")
    assert passes(case, "High") is True
    assert passes(case, "Medium") is False
    assert passes(case, None) is False


def test_critical_case_requires_exact_critical():
    case = _case(customer="Betsy Sandstrom", expected="Critical")
    assert passes(case, "Critical") is True
    assert passes(case, "High") is False


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


# --- run_eval offline mode: no network, no DB writes -------------------------

def test_offline_mode_makes_no_real_api_call():
    with patch("integrations.anthropic_client.call_with_tool") as mock_call:
        result = run_eval(offline=True)
    mock_call.assert_not_called()
    # 9 calibration cases minus the 1 summary-only item (Sandstrom's
    # pre-cancellation scenario), which becomes history, never a graded row.
    assert len(result["rows"]) == 8


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


def test_real_run_without_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import pytest
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        run_eval(offline=False)


def test_all_customers_and_dates_covered():
    result = run_eval(offline=True)
    customers = {row["customer"] for row in result["rows"]}
    assert customers == {"Julie Harrison", "Danielle Ewen", "Caroline Day", "Betsy Sandstrom"}


def test_summary_only_item_is_never_an_independent_graded_row():
    result = run_eval(offline=True)
    sandstrom_rows = [r for r in result["rows"] if r["customer"] == "Betsy Sandstrom"]
    # Only the dated (2026-10-20) Sandstrom case is graded - the
    # pre-cancellation summary becomes history for it, not its own row.
    assert len(sandstrom_rows) == 1
    assert sandstrom_rows[0]["date"] == "2026-10-20"


def test_summary_only_item_feeds_sandstroms_history():
    seen_histories = []

    def fake_analyze_one(message, history, open_flag, **kwargs):
        if message["body_text"].startswith("We'll let you know"):
            seen_histories.append(list(history))
        return {
            "status": "ok", "sentiment": "neutral", "suggested_severity": None,
            "summary": "s", "confidence": 0.5, "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    with patch("agents.casey.analyze.analyze_one", fake_analyze_one):
        run_eval(offline=True)
    assert len(seen_histories) == 1
    assert any(h.get("is_summary") for h in seen_histories[0])


def test_history_accumulates_within_customer_chronologically():
    # Patch analyze_one to capture the history list it was called with,
    # confirming each later case for the same customer sees the earlier ones.
    seen_history_lengths = []

    def fake_analyze_one(message, history, open_flag, **kwargs):
        seen_history_lengths.append(len(history))
        return {
            "status": "ok", "sentiment": "neutral", "suggested_severity": None,
            "summary": "s", "confidence": 0.5, "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    with patch("agents.casey.analyze.analyze_one", fake_analyze_one):
        run_eval(offline=True)
    # Julie Harrison has 3 examples -> history lengths 0, 1, 2 for that customer
    assert 0 in seen_history_lengths
    assert any(n >= 2 for n in seen_history_lengths)
