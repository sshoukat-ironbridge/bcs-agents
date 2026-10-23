from datetime import date

from agents.oca.checklist import build_checklist

EMPTY_DEADLINE_RESULT = {"overdue": [], "soon": [], "no_deadline_count": 0}
EMPTY_PM_ISSUES = {"blank": [], "unrecognized": []}
COUNTS = {"to_start": 3, "in_process": 5, "on_hold": 1, "retired_this_run": 0}


def test_all_sections_empty_sends_nothing_needs_attention():
    result = build_checklist([], EMPTY_DEADLINE_RESULT, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert result["is_empty"] is True
    assert "nothing needs attention" in result["slack_text"]
    assert "nothing needs attention" in result["html"]
    # Footer counts still present even when empty.
    assert "To Start: 3" in result["slack_text"]


def test_deadline_change_section_appears():
    changes = [{"client_name": "Acme", "job_type": "Deck", "pm_name": "Gustavo", "old_deadline": "2026-10-01", "new_deadline": "2026-10-15"}]
    result = build_checklist(changes, EMPTY_DEADLINE_RESULT, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert result["is_empty"] is False
    assert "Deadline to Start changed" in result["slack_text"]
    assert "Acme" in result["slack_text"]
    assert "2026-10-01" in result["slack_text"] and "2026-10-15" in result["slack_text"]


def test_overdue_and_soon_sections_appear_with_reason_action_clears():
    deadline_result = {
        "overdue": [{"client_name": "Acme", "job_type": "Deck", "pm_name": "Gustavo", "deadline": date(2026, 10, 18), "days_until": -3}],
        "soon": [{"client_name": "Beta", "job_type": "Roof", "pm_name": "unassigned", "deadline": date(2026, 10, 25), "days_until": 4}],
        "no_deadline_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert "Start deadline passed, job still in To Start" in result["slack_text"]
    assert "Starting soon (within 7 days)" in result["slack_text"]
    for keyword in ("Reason:", "Action:", "Clears when:"):
        assert keyword in result["slack_text"]


def test_pm_sections_appear():
    pm_issues = {
        "blank": [{"client_name": "Gamma", "job_type": "Fence"}],
        "unrecognized": [{"raw_name": "Mystery Person", "job_count": 2, "clients": ["Delta", "Epsilon"]}],
    }
    result = build_checklist([], EMPTY_DEADLINE_RESULT, pm_issues, COUNTS, warning_days=7)
    assert "No PM assigned" in result["slack_text"]
    assert "PM name not recognized" in result["slack_text"]
    assert "Add this person in PM config or fix the sheet" in result["slack_text"]
    assert "Mystery Person" in result["slack_text"]


def test_slack_text_truncates_to_25_item_lines_with_more_count():
    overdue = [
        {"client_name": f"Client{i}", "job_type": "Deck", "pm_name": "Gustavo", "deadline": date(2026, 10, 1), "days_until": -5}
        for i in range(30)
    ]
    deadline_result = {"overdue": overdue, "soon": [], "no_deadline_count": 0}
    pm_issues = {
        "blank": [{"client_name": "X", "job_type": "Y"}],
        "unrecognized": [{"raw_name": "Bob", "job_count": 2, "clients": ["A", "B"]}],
    }
    result = build_checklist([], deadline_result, pm_issues, COUNTS, warning_days=7)
    item_lines = [l for l in result["slack_text"].splitlines() if l.startswith("- ")]
    assert len(item_lines) == 25
    assert "+7 more" in result["slack_text"]


def test_html_is_untrimmed_even_when_slack_is_truncated():
    overdue = [
        {"client_name": f"Client{i}", "job_type": "Deck", "pm_name": "Gustavo", "deadline": date(2026, 10, 1), "days_until": -5}
        for i in range(30)
    ]
    deadline_result = {"overdue": overdue, "soon": [], "no_deadline_count": 0}
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    for i in range(30):
        assert f"Client{i}" in result["html"]
