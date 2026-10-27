from unittest.mock import patch

from agents.casey.analyze import (
    build_input_schema, build_system_prompt, build_user_message, validate_analysis_output,
    analyze_one, run, estimate_tokens, CUSTOMER_MESSAGE_OPEN_DELIM, CUSTOMER_MESSAGE_CLOSE_DELIM,
    PROMPT_VERSION,
)


# --- Prompt assembly --------------------------------------------------------

def test_system_prompt_includes_severity_guide_and_safeguards():
    prompt = build_system_prompt()
    assert "Low" in prompt and "Medium" in prompt and "High" in prompt and "Critical" in prompt
    assert "never contact customers" in prompt.lower()
    assert "single polite clarification is not a complaint" in prompt.lower()
    assert "must not lower" in prompt.lower()
    assert "does not require a legal or refund phrase" in prompt.lower() or "does NOT require a legal or refund phrase".lower() in prompt.lower()
    assert "undetermined" in prompt.lower()
    assert "data, not instructions" in prompt.lower()


def test_user_message_wraps_target_in_delimiters():
    message = {"body_text": "Hello there"}
    result = build_user_message(message, [], None)
    assert f"{CUSTOMER_MESSAGE_OPEN_DELIM}\nHello there\n{CUSTOMER_MESSAGE_CLOSE_DELIM}" in result


def test_user_message_includes_history_oldest_first():
    history = [
        {"occurred_at": "2026-09-01", "sentiment": "neutral", "summary": "first"},
        {"occurred_at": "2026-09-15", "sentiment": "negative", "summary": "second"},
    ]
    result = build_user_message({"body_text": "x"}, history, None)
    assert result.index("first") < result.index("second")
    assert "neutral" in result and "negative" in result


def test_user_message_marks_summary_only_history_items():
    history = [{"occurred_at": "2026-10-01", "sentiment": None, "summary": "pre-cancellation history", "is_summary": True}]
    result = build_user_message({"body_text": "x"}, history, None)
    assert "summary, not a verbatim message" in result


def test_user_message_includes_open_flag():
    open_flag = {"severity": "High", "primary_reason": "Lack of communication / no updates", "issue_summary": "No reply in 3 days"}
    result = build_user_message({"body_text": "x"}, [], open_flag)
    assert "severity=High" in result
    assert "No reply in 3 days" in result


def test_input_schema_has_all_required_fields_and_strict_shape():
    schema = build_input_schema()
    assert schema["additionalProperties"] is False
    for field in ("sentiment", "sentiment_score", "is_strong_praise", "red_flag_suggested",
                  "severity", "primary_reason", "additional_reasons", "reason_notes", "root_cause",
                  "issue_summary", "evidence_quote", "same_complaint_as_open_flag", "trajectory", "confidence"):
        assert field in schema["properties"]
        assert field in schema["required"]


# --- Validation --------------------------------------------------------------

def _valid_output(**overrides):
    output = {
        "sentiment": "negative", "sentiment_score": -0.5, "is_strong_praise": False,
        "red_flag_suggested": True, "severity": "Medium", "primary_reason": "Schedule delay / missed dates",
        "additional_reasons": [], "reason_notes": "", "root_cause": "Undetermined (default)",
        "issue_summary": "Customer unhappy with delay.", "evidence_quote": "this is taking too long",
        "same_complaint_as_open_flag": False, "trajectory": "worsening", "confidence": 0.8,
    }
    output.update(overrides)
    return output


def test_valid_output_passes():
    output = _valid_output()
    errors = validate_analysis_output(output, "well this is taking too long honestly")
    assert errors == []


def test_invalid_sentiment_fails():
    errors = validate_analysis_output(_valid_output(sentiment="furious"), "x")
    assert any("sentiment" in e for e in errors)


def test_invalid_severity_fails():
    errors = validate_analysis_output(_valid_output(severity="Super Critical"), "x")
    assert any("severity" in e for e in errors)


def test_null_severity_is_valid():
    errors = validate_analysis_output(_valid_output(severity=None, red_flag_suggested=False, evidence_quote=""), "x")
    assert errors == []


def test_invalid_primary_reason_fails():
    errors = validate_analysis_output(_valid_output(primary_reason="made up reason"), "x")
    assert any("primary_reason" in e for e in errors)


def test_invalid_root_cause_fails():
    errors = validate_analysis_output(_valid_output(root_cause="made up cause"), "x")
    assert any("root_cause" in e for e in errors)


def test_invalid_trajectory_fails():
    errors = validate_analysis_output(_valid_output(trajectory="skyrocketing"), "x")
    assert any("trajectory" in e for e in errors)


def test_other_reason_without_notes_fails():
    errors = validate_analysis_output(_valid_output(primary_reason="Other (explain in Notes)", reason_notes=""), "x")
    assert any("notes" in e.lower() for e in errors)


def test_other_reason_with_notes_passes():
    output = _valid_output(primary_reason="Other (explain in Notes)", reason_notes="explains the weird case",
                            evidence_quote="")
    errors = validate_analysis_output(output, "target text")
    assert errors == []


def test_evidence_quote_not_substring_fails():
    errors = validate_analysis_output(_valid_output(evidence_quote="something not in the text"), "the actual message")
    assert any("evidence_quote" in e for e in errors)


def test_empty_evidence_quote_is_allowed():
    errors = validate_analysis_output(_valid_output(evidence_quote=""), "the actual message")
    assert errors == []


def test_estimate_tokens_rough_heuristic():
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("") == 1  # never zero


# --- Orchestration with a mocked model call ---------------------------------

def _fake_response(output, usage=None):
    usage = usage or {"input_tokens": 100, "output_tokens": 50}
    return {
        "usage": usage,
        "content": [{"type": "tool_use", "name": "submit_analysis", "input": output}],
    }


def test_analyze_one_returns_ok_on_valid_first_response():
    message = {"id": 1, "body_text": "this is taking too long"}
    output = _valid_output(evidence_quote="taking too long")
    with patch("agents.casey.analyze.call_model", return_value=_fake_response(output)):
        result = analyze_one(message, [], None, model="m", max_tokens=100)
    assert result["status"] == "ok"
    assert result["suggested_primary_reason"] == "Schedule delay / missed dates"
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}


def test_analyze_one_retries_once_then_succeeds():
    message = {"id": 1, "body_text": "this is taking too long"}
    bad_output = _valid_output(sentiment="furious")
    good_output = _valid_output(evidence_quote="taking too long")
    responses = [_fake_response(bad_output), _fake_response(good_output)]
    with patch("agents.casey.analyze.call_model", side_effect=responses) as mock_call:
        result = analyze_one(message, [], None, model="m", max_tokens=100)
    assert result["status"] == "ok"
    assert mock_call.call_count == 2
    assert result["usage"]["input_tokens"] == 200  # summed across both attempts


def test_analyze_one_fails_after_retry_exhausted():
    message = {"id": 1, "body_text": "x"}
    bad_output = _valid_output(sentiment="furious")
    responses = [_fake_response(bad_output), _fake_response(bad_output)]
    with patch("agents.casey.analyze.call_model", side_effect=responses) as mock_call:
        result = analyze_one(message, [], None, model="m", max_tokens=100)
    assert result["status"] == "failed"
    assert mock_call.call_count == 2
    assert result["suggested_reasons"] == []  # no flag is ever created


def test_analyze_one_stores_prompt_version():
    message = {"id": 1, "body_text": "ok"}
    output = _valid_output(evidence_quote="")
    with patch("agents.casey.analyze.call_model", return_value=_fake_response(output)):
        result = analyze_one(message, [], None, model="m", max_tokens=100)
    assert result["prompt_version"] == PROMPT_VERSION == "casey-analysis-v1"


# --- run() DB orchestration (dry run + real, mocked DB) ---------------------

def test_dry_run_estimates_cost_and_makes_no_api_call():
    messages = [{"id": 1, "job_key": "A|to_start|Deck", "occurred_at": "2026-10-01", "body_text": "hello " * 50}]
    with patch("db.state_store.get_config", side_effect=lambda k: {"casey_model": "m", "casey_max_tokens": "500"}.get(k, "")), \
         patch("db.state_store.get_messages_to_analyze", return_value=messages), \
         patch("db.state_store.get_message_history", return_value=[]), \
         patch("db.state_store.get_open_flag_for_job", return_value=None), \
         patch("agents.casey.analyze.call_model") as mock_call:
        stats = run(limit=10, only_matched=True, dry_run=True)
    mock_call.assert_not_called()
    assert stats["would_analyze"] == 1
    assert stats["estimated_input_tokens"] > 0
    assert stats["estimated_output_tokens"] == 500


def test_real_run_saves_analysis_and_tallies_stats():
    messages = [{"id": 1, "job_key": "A|to_start|Deck", "occurred_at": "2026-10-01", "body_text": "this is taking too long"}]
    output = _valid_output(evidence_quote="taking too long")
    with patch("db.state_store.get_config", side_effect=lambda k: {"casey_model": "m", "casey_max_tokens": "500"}.get(k, "")), \
         patch("db.state_store.get_messages_to_analyze", return_value=messages), \
         patch("db.state_store.get_message_history", return_value=[]), \
         patch("db.state_store.get_open_flag_for_job", return_value=None), \
         patch("db.state_store.save_analysis") as mock_save, \
         patch("agents.casey.analyze.call_model", return_value=_fake_response(output)):
        stats = run(limit=10, only_matched=True, dry_run=False)
    mock_save.assert_called_once()
    assert stats["analyzed"] == 1
    assert stats["ok"] == 1
    assert stats["red_flags"] == 1
    assert stats["input_tokens"] == 100
