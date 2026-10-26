from datetime import date

from agents.oca.checklist import build_checklist, SLACK_PER_SECTION_CAP

EMPTY_DEADLINE_RESULT = {"overdue": [], "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0}
EMPTY_PM_ISSUES = {"blank": [], "unrecognized": []}
COUNTS = {"to_start": 3, "in_process": 5, "on_hold": 1, "retired_this_run": 0}


def _overdue_item(client, days_ago, job_type="Deck", pm="Gustavo"):
    return {"client_name": client, "job_type": job_type, "pm_name": pm,
            "deadline": date(2026, 10, 1), "days_until": -days_ago}


def _soon_item(client, deadline, job_type="Deck", pm="Gustavo"):
    return {"client_name": client, "job_type": job_type, "pm_name": pm,
            "deadline": deadline, "days_until": (deadline - date(2026, 10, 21)).days}


def test_all_sections_empty_sends_nothing_needs_attention():
    result = build_checklist([], EMPTY_DEADLINE_RESULT, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert result["is_empty"] is True
    assert "nothing needs attention" in result["slack_text"]
    assert "nothing needs attention" in result["html"]
    assert "To Start: 3" in result["slack_text"]
    assert result["section_counts"] == {}


def test_section_order_matches_spec():
    deadline_changes = [{"client_name": "A", "job_type": "Deck", "pm_name": "G", "old_deadline": "x", "new_deadline": "y"}]
    deadline_result = {
        "overdue": [_overdue_item("B", 3)],
        "soon": [_soon_item("C", date(2026, 10, 25))],
        "no_deadline_to_start": [{"client_name": "F", "job_type": "Deck", "pm_name": "G"}],
        "no_deadline_in_process_count": 2,
    }
    pm_issues = {
        "blank": [{"client_name": "D", "job_type": "Deck"}],
        "unrecognized": [{"raw_name": "E", "job_count": 1, "clients": ["E Client"]}],
    }
    result = build_checklist(deadline_changes, deadline_result, pm_issues, COUNTS, warning_days=7)

    titles_in_order = [
        "Deadline to Start changed",
        "Start deadline passed",
        "Starting soon",
        "No PM assigned",
        "PM name not recognized",
        "No readable deadline",
    ]
    positions = [result["slack_text"].index(t) for t in titles_in_order]
    assert positions == sorted(positions)


def test_reason_action_clears_once_per_section_not_per_line():
    deadline_result = {
        "overdue": [_overdue_item("A", 1), _overdue_item("B", 2), _overdue_item("C", 3)],
        "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert result["slack_text"].count("Reason:") == 1
    assert result["slack_text"].count("Action:") == 1
    assert result["slack_text"].count("Clears when:") == 1
    # HTML also states it once per section, not per item.
    assert result["html"].count("Reason:") == 1


def test_overdue_item_line_format():
    deadline_result = {
        "overdue": [{"client_name": "Acme", "job_type": "Deck Build", "pm_name": "Gustavo",
                      "deadline": date(2026, 10, 18), "days_until": -3}],
        "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert "Acme (Deck Build, PM: Gustavo): deadline 2026-10-18 (3 days ago)" in result["slack_text"]


def test_soon_item_line_format_uses_in_n_days():
    deadline_result = {
        "overdue": [],
        "soon": [{"client_name": "Beta", "job_type": "Roof", "pm_name": "unassigned",
                   "deadline": date(2026, 10, 25), "days_until": 4}],
        "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert "Beta (Roof, PM: unassigned): deadline 2026-10-25 (in 4 days)" in result["slack_text"]


def test_job_type_truncated_to_60_chars_with_ellipsis():
    long_job_type = "X" * 80
    deadline_result = {
        "overdue": [{"client_name": "Acme", "job_type": long_job_type, "pm_name": "G",
                      "deadline": date(2026, 10, 18), "days_until": -1}],
        "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert long_job_type not in result["slack_text"]
    assert "X" * 59 + "…" in result["slack_text"]


def test_overdue_sorted_most_days_overdue_first():
    deadline_result = {
        "overdue": [_overdue_item("Least", 1), _overdue_item("Most", 10), _overdue_item("Mid", 5)],
        "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    text = result["slack_text"]
    assert text.index("Most") < text.index("Mid") < text.index("Least")


def test_soon_sorted_by_deadline_ascending_then_client_name():
    deadline_result = {
        "overdue": [],
        "soon": [
            _soon_item("Zebra", date(2026, 10, 22)),
            _soon_item("Alpha", date(2026, 10, 22)),  # same date as Zebra -> name tiebreak
            _soon_item("Middle", date(2026, 10, 25)),
        ],
        "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    text = result["slack_text"]
    assert text.index("Alpha") < text.index("Zebra") < text.index("Middle")


def test_per_section_slack_truncation_with_more_count():
    overdue = [_overdue_item(f"Client{i}", i + 1) for i in range(SLACK_PER_SECTION_CAP + 3)]
    deadline_result = {"overdue": overdue, "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0}
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    item_lines = [l for l in result["slack_text"].splitlines() if l.startswith("- ")]
    assert len(item_lines) == SLACK_PER_SECTION_CAP
    assert "+3 more, see the email" in result["slack_text"]


def test_every_nonempty_section_appears_even_when_another_is_truncated():
    overdue = [_overdue_item(f"Client{i}", i + 1) for i in range(SLACK_PER_SECTION_CAP + 5)]
    deadline_result = {"overdue": overdue, "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0}
    pm_issues = {"blank": [{"client_name": "Solo", "job_type": "Fence"}], "unrecognized": []}
    result = build_checklist([], deadline_result, pm_issues, COUNTS, warning_days=7)
    assert "No PM assigned" in result["slack_text"]
    assert "Solo" in result["slack_text"]


def test_html_is_untrimmed_even_when_slack_is_truncated():
    overdue = [_overdue_item(f"Client{i}", i + 1) for i in range(SLACK_PER_SECTION_CAP + 5)]
    deadline_result = {"overdue": overdue, "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0}
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    for i in range(SLACK_PER_SECTION_CAP + 5):
        assert f"Client{i}" in result["html"]
    assert "+N more" not in result["html"]


def test_no_readable_deadline_section_names_to_start_only():
    deadline_result = {
        "overdue": [], "soon": [],
        "no_deadline_to_start": [{"client_name": "Unreadable Co", "job_type": "Deck", "pm_name": "G"}],
        "no_deadline_in_process_count": 4,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert "No readable deadline" in result["slack_text"]
    assert "Unreadable Co" in result["slack_text"]
    # In Process no-deadline jobs are never named — only counted in the footer.
    assert "No readable deadline — To Start: 1, In Process: 4." in result["slack_text"]


def test_pm_sections_use_header_level_text():
    pm_issues = {
        "blank": [{"client_name": "Gamma", "job_type": "Fence"}],
        "unrecognized": [{"raw_name": "Mystery Person", "job_count": 2, "clients": ["Delta", "Epsilon"]}],
    }
    result = build_checklist([], EMPTY_DEADLINE_RESULT, pm_issues, COUNTS, warning_days=7)
    assert "No PM assigned" in result["slack_text"]
    assert "PM name not recognized" in result["slack_text"]
    assert "Add this person in PM config or fix the sheet" in result["slack_text"]
    assert "Mystery Person" in result["slack_text"]


def test_section_counts_reflect_item_counts():
    deadline_result = {
        "overdue": [_overdue_item("A", 1), _overdue_item("B", 2)],
        "soon": [_soon_item("C", date(2026, 10, 25))],
        "no_deadline_to_start": [], "no_deadline_in_process_count": 0,
    }
    result = build_checklist([], deadline_result, EMPTY_PM_ISSUES, COUNTS, warning_days=7)
    assert result["section_counts"]["overdue"] == 2
    assert result["section_counts"]["soon"] == 1
    assert "no_pm" not in result["section_counts"]
