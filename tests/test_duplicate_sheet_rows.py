from unittest.mock import patch

from agents.oca.main import collapse_duplicate_sheet_rows, _sync_all


def _job(client_name="Caroline Day", sheet_tab="to_start", primary_job_type="Landscaping",
         deadline_to_start="", pm_name="", **overrides):
    job = {
        "client_name": client_name,
        "sheet_tab": sheet_tab,
        "primary_job_type": primary_job_type,
        "job_type": primary_job_type,
        "deadline_to_start": deadline_to_start,
        "pm_name": pm_name,
    }
    job.update(overrides)
    return job


# ── collapse_duplicate_sheet_rows() ─────────────────────────────────────

def test_no_duplicates_returns_all_rows_unchanged():
    jobs = [_job(client_name="A"), _job(client_name="B")]
    collapsed, dupes = collapse_duplicate_sheet_rows(jobs)
    assert collapsed == jobs
    assert dupes == []


def test_duplicate_pair_collapses_to_last_row():
    first = _job(deadline_to_start="7/9/2026")
    second = _job(deadline_to_start="8/13/2026")
    collapsed, dupes = collapse_duplicate_sheet_rows([first, second])

    assert len(collapsed) == 1
    assert collapsed[0] is second  # last row wins
    assert len(dupes) == 1
    assert dupes[0]["key"] == ("Caroline Day", "to_start", "Landscaping")
    assert dupes[0]["rows"] == [first, second]


def test_different_tabs_or_job_types_are_not_duplicates():
    jobs = [
        _job(sheet_tab="to_start"),
        _job(sheet_tab="in_process"),  # different tab -> different key
        _job(primary_job_type="Fence"),  # different job type -> different key
    ]
    collapsed, dupes = collapse_duplicate_sheet_rows(jobs)
    assert len(collapsed) == 3
    assert dupes == []


def test_preserves_relative_order_of_distinct_keys():
    jobs = [_job(client_name="Z"), _job(client_name="A"), _job(client_name="M")]
    collapsed, _ = collapse_duplicate_sheet_rows(jobs)
    assert [j["client_name"] for j in collapsed] == ["Z", "A", "M"]


# ── Integration: a duplicate pair must never produce a change on its own ──

def test_duplicate_pair_produces_no_deadline_change_alert(monkeypatch):
    """Reproduces the exact Caroline Day scenario: DB has 8/13/2026 stored
    from a previous run; this run's sheet has two rows for the same key,
    [7/9/2026 first, 8/13/2026 second]. Since the last row (8/13) matches
    the stored value, there must be NO deadline-change alert — not even
    a transient one for the first (overwritten) row."""
    sheet_jobs = [
        _job(deadline_to_start="7/9/2026"),
        _job(deadline_to_start="8/13/2026"),
    ]
    stored_row = {"client_name": "Caroline Day", "sheet_tab": "to_start",
                  "primary_job_type": "Landscaping", "deadline_to_start": "8/13/2026"}

    with patch("db.state_store.get_all_active_jobs", return_value=[stored_row]), \
         patch("agents.oca.main.send_deadline_change_alert") as mock_alert, \
         patch("agents.oca.main.upsert_active_job"), \
         patch("agents.oca.main.set_config"):
        result = _sync_all(sheet_jobs)

    mock_alert.assert_not_called()
    assert result["deadline_changes"] == []


def test_duplicate_pair_warning_is_logged_once(monkeypatch, caplog):
    import logging
    sheet_jobs = [
        _job(deadline_to_start="7/9/2026", pm_name="HOLD/Daniel"),
        _job(deadline_to_start="8/13/2026", pm_name="HOLD/Daniel"),
    ]
    with patch("db.state_store.get_all_active_jobs", return_value=[]), \
         patch("agents.oca.main.send_deadline_change_alert"), \
         patch("agents.oca.main.upsert_active_job"), \
         patch("agents.oca.main.set_config"), \
         caplog.at_level(logging.WARNING, logger="oca"):
        _sync_all(sheet_jobs)

    warnings = [r for r in caplog.records if "Duplicate sheet rows" in r.message]
    assert len(warnings) == 1
    assert "7/9/2026" in warnings[0].message and "8/13/2026" in warnings[0].message


def test_genuinely_new_deadline_on_the_winning_row_still_alerts(monkeypatch):
    """A duplicate pair must never produce a change ON ITS OWN, but if the
    LAST (winning) row's value genuinely differs from history, that's a
    real change and must still alert exactly once."""
    sheet_jobs = [
        _job(deadline_to_start="8/13/2026"),  # same as history — irrelevant, gets overwritten
        _job(deadline_to_start="9/1/2026"),   # winning row — genuinely new value
    ]
    stored_row = {"client_name": "Caroline Day", "sheet_tab": "to_start",
                  "primary_job_type": "Landscaping", "deadline_to_start": "8/13/2026"}

    with patch("db.state_store.get_all_active_jobs", return_value=[stored_row]), \
         patch("agents.oca.main.send_deadline_change_alert") as mock_alert, \
         patch("agents.oca.main.upsert_active_job"), \
         patch("agents.oca.main.set_config"):
        result = _sync_all(sheet_jobs)

    mock_alert.assert_called_once()
    assert len(result["deadline_changes"]) == 1
    assert result["deadline_changes"][0]["new_deadline"] == "9/1/2026"


# ── Idempotency: two consecutive runs with a duplicate pair ────────────

def test_two_consecutive_syncs_with_duplicate_pair_are_idempotent():
    """Simulates _sync_all() running twice back to back against a
    persistent fake DB, with fixture sheet_jobs that include the exact
    Caroline Day duplicate pair. Per spec: the second run must show 0
    deadline changes."""
    fake_db: dict[tuple, dict] = {}

    def fake_get_all_active_jobs():
        return [dict(v) for v in fake_db.values()]

    def fake_upsert_active_job(job):
        key = (job["client_name"], job.get("sheet_tab") or "", job.get("primary_job_type") or "")
        fake_db[key] = dict(job)

    def make_sheet_jobs():
        return [
            _job(deadline_to_start="7/9/2026"),
            _job(deadline_to_start="8/13/2026"),
        ]

    with patch("db.state_store.get_all_active_jobs", side_effect=fake_get_all_active_jobs), \
         patch("agents.oca.main.upsert_active_job", side_effect=fake_upsert_active_job), \
         patch("agents.oca.main.send_deadline_change_alert") as mock_alert, \
         patch("agents.oca.main.set_config"):
        result1 = _sync_all(make_sheet_jobs())
        result2 = _sync_all(make_sheet_jobs())

    print(f"Run 1 summary: synced={result1['synced']}, errors={result1['errors']}, "
          f"deadline_changes={len(result1['deadline_changes'])}")
    print(f"Run 2 summary: synced={result2['synced']}, errors={result2['errors']}, "
          f"deadline_changes={len(result2['deadline_changes'])}")

    # Run 1: no prior history in fake_db -> old_deadline_raw is empty for
    # the winning row, so no change is detected (the code only alerts when
    # BOTH old and new parse to a date).
    assert result1["deadline_changes"] == []
    # Run 2: fake_db now holds 8/13/2026 from run 1 (the winning row both
    # times) -> still the same value -> still 0 deadline changes.
    assert result2["deadline_changes"] == []
    assert mock_alert.call_count == 0
    # Confirms the DB ends up holding the winning (last) row's value both times.
    assert fake_db[("Caroline Day", "to_start", "Landscaping")]["deadline_to_start"] == "8/13/2026"
