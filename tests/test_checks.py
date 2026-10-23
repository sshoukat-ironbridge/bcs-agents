from datetime import date

from agents.oca.checks import check_deadline_to_start, classify_pm_issues


def _job(**overrides):
    base = {
        "client_name": "Test Client",
        "job_type": "Deck",
        "pm_name": "Gustavo Zuluaga",
        "sheet_tab": "to_start",
        "comms_hold": False,
        "deadline_to_start": "",
    }
    base.update(overrides)
    return base


TODAY = date(2026, 10, 21)


# ── check_deadline_to_start ──────────────────────────────────────────────

def test_overdue_job_is_flagged_with_negative_days():
    jobs = [_job(deadline_to_start="2026-10-18")]  # 3 days before TODAY
    result = check_deadline_to_start(jobs, TODAY)
    assert len(result["overdue"]) == 1
    assert result["overdue"][0]["days_until"] == -3
    assert result["soon"] == []


def test_soon_job_within_warning_window():
    jobs = [_job(deadline_to_start="2026-10-25")]  # 4 days after TODAY
    result = check_deadline_to_start(jobs, TODAY, warning_days=7)
    assert len(result["soon"]) == 1
    assert result["soon"][0]["days_until"] == 4
    assert result["overdue"] == []


def test_boundary_day_exactly_warning_days_is_soon():
    jobs = [_job(deadline_to_start="2026-10-28")]  # exactly 7 days after TODAY
    result = check_deadline_to_start(jobs, TODAY, warning_days=7)
    assert len(result["soon"]) == 1
    assert result["soon"][0]["days_until"] == 7


def test_boundary_day_one_past_warning_days_is_not_listed():
    jobs = [_job(deadline_to_start="2026-10-29")]  # 8 days after TODAY
    result = check_deadline_to_start(jobs, TODAY, warning_days=7)
    assert result["soon"] == []
    assert result["overdue"] == []
    assert result["no_deadline_count"] == 0


def test_today_itself_counts_as_soon_not_overdue():
    jobs = [_job(deadline_to_start="2026-10-21")]  # == TODAY
    result = check_deadline_to_start(jobs, TODAY, warning_days=7)
    assert len(result["soon"]) == 1
    assert result["soon"][0]["days_until"] == 0
    assert result["overdue"] == []


def test_missing_deadline_only_counted_not_listed():
    jobs = [_job(deadline_to_start="")]
    result = check_deadline_to_start(jobs, TODAY)
    assert result["overdue"] == []
    assert result["soon"] == []
    assert result["no_deadline_count"] == 1


def test_unparseable_deadline_counted_as_no_deadline():
    jobs = [_job(deadline_to_start="not a date")]
    result = check_deadline_to_start(jobs, TODAY)
    assert result["no_deadline_count"] == 1


def test_held_jobs_are_skipped_entirely():
    jobs = [_job(deadline_to_start="2026-10-18", comms_hold=True)]
    result = check_deadline_to_start(jobs, TODAY)
    assert result["overdue"] == []
    assert result["no_deadline_count"] == 0


def test_non_to_start_jobs_are_skipped():
    jobs = [_job(deadline_to_start="2026-10-18", sheet_tab="in_process")]
    result = check_deadline_to_start(jobs, TODAY)
    assert result["overdue"] == []
    assert result["no_deadline_count"] == 0


def test_unassigned_pm_label_when_blank():
    jobs = [_job(deadline_to_start="2026-10-18", pm_name="")]
    result = check_deadline_to_start(jobs, TODAY)
    assert result["overdue"][0]["pm_name"] == "unassigned"


# ── classify_pm_issues ───────────────────────────────────────────────────

# A name with no entry in db.state_store._PM_NAME_ALIASES, so find_pm()
# matches it on a direct, case-insensitive full_name comparison — keeps
# this test isolated from the production alias table's specifics.
_PM_CONFIG = [{"full_name": "Taylor Example", "email": "taylor@bluecollarscholars.net"}]


def test_blank_pm_is_classified():
    jobs = [_job(pm_name="")]
    result = classify_pm_issues(jobs, pm_config=_PM_CONFIG)
    assert len(result["blank"]) == 1
    assert result["unrecognized"] == []


def test_unrecognized_pm_grouped_by_name_with_count():
    jobs = [
        _job(client_name="Client A", pm_name="Some Rando"),
        _job(client_name="Client B", pm_name="Some Rando"),
    ]
    result = classify_pm_issues(jobs, pm_config=_PM_CONFIG)
    assert result["blank"] == []
    assert len(result["unrecognized"]) == 1
    entry = result["unrecognized"][0]
    assert entry["raw_name"] == "Some Rando"
    assert entry["job_count"] == 2
    assert set(entry["clients"]) == {"Client A", "Client B"}


def test_resolved_pm_is_not_flagged():
    jobs = [_job(pm_name="Taylor Example")]
    result = classify_pm_issues(jobs, pm_config=_PM_CONFIG)
    assert result["blank"] == []
    assert result["unrecognized"] == []


def test_held_job_is_skipped_even_with_blank_pm():
    jobs = [_job(pm_name="", comms_hold=True)]
    result = classify_pm_issues(jobs, pm_config=_PM_CONFIG)
    assert result["blank"] == []
    assert result["unrecognized"] == []
