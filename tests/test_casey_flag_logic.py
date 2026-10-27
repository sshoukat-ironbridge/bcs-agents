from agents.casey.flag_logic import decide_action, is_same_complaint, find_matching_flag


def _flag(job_key="A|to_start|Deck", primary_reason="Rework required", severity="Medium"):
    return {"job_key": job_key, "primary_reason": primary_reason, "severity": severity}


def _candidate(job_key="A|to_start|Deck", primary_reason="Rework required",
                severity="Medium", red_flag_suggested=True, sentiment="negative"):
    return {"job_key": job_key, "primary_reason": primary_reason, "severity": severity,
            "red_flag_suggested": red_flag_suggested, "sentiment": sentiment}


def test_no_existing_flags_and_red_flag_creates():
    result = decide_action([], _candidate())
    assert result == {"action": "create", "matched_flag": None}


def test_no_existing_flags_and_no_red_flag_is_no_op():
    result = decide_action([], _candidate(red_flag_suggested=False, sentiment="neutral"))
    assert result["action"] == "no_op"


def test_different_complaint_on_same_job_creates_separate():
    existing = [_flag(primary_reason="Schedule delay / missed dates")]
    candidate = _candidate(primary_reason="Workmanship / quality of work")
    result = decide_action(existing, candidate)
    assert result["action"] == "create_separate"
    assert result["matched_flag"] is None


def test_same_complaint_higher_severity_escalates():
    existing = [_flag(severity="Medium")]
    candidate = _candidate(severity="High")
    result = decide_action(existing, candidate)
    assert result["action"] == "escalate"
    assert result["matched_flag"] == existing[0]


def test_same_complaint_same_severity_is_no_op():
    existing = [_flag(severity="Medium")]
    candidate = _candidate(severity="Medium")
    result = decide_action(existing, candidate)
    assert result["action"] == "no_op"


def test_same_complaint_lower_severity_does_not_auto_lower():
    existing = [_flag(severity="High")]
    candidate = _candidate(severity="Low")
    result = decide_action(existing, candidate)
    # Lowering is a human-only action; Casey never does it automatically.
    assert result["action"] != "escalate"
    assert result["action"] == "no_op"


def test_same_complaint_improving_sentiment_notes_but_never_closes_or_escalates():
    existing = [_flag(severity="High")]
    candidate = _candidate(severity="High", sentiment="positive")
    result = decide_action(existing, candidate)
    assert result["action"] == "note_improvement"
    assert result["matched_flag"] == existing[0]


def test_same_complaint_no_longer_a_red_flag_notes_improvement():
    existing = [_flag()]
    candidate = _candidate(red_flag_suggested=False, sentiment="neutral")
    result = decide_action(existing, candidate)
    assert result["action"] == "note_improvement"


def test_is_same_complaint_requires_both_job_key_and_reason():
    flag = _flag()
    assert is_same_complaint(flag, _candidate()) is True
    assert is_same_complaint(flag, _candidate(job_key="B|to_start|Deck")) is False
    assert is_same_complaint(flag, _candidate(primary_reason="Other (explain in Notes)")) is False


def test_find_matching_flag_returns_first_match_or_none():
    flags = [_flag(job_key="X"), _flag(job_key="A|to_start|Deck")]
    assert find_matching_flag(flags, _candidate()) == flags[1]
    assert find_matching_flag([], _candidate()) is None


def test_configurable_same_complaint_field():
    existing = [{"job_key": "A|to_start|Deck", "category": "billing", "severity": "Medium"}]
    candidate = {"job_key": "A|to_start|Deck", "category": "billing", "severity": "High",
                 "red_flag_suggested": True, "sentiment": "negative"}
    result = decide_action(existing, candidate, same_complaint_by="category")
    assert result["action"] == "escalate"
