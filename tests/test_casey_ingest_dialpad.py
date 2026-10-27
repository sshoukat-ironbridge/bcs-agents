from datetime import timedelta
from unittest.mock import patch

from agents.casey.ingest_dialpad import run, _parse_since, is_enabled

PMS = [
    {"id": 1, "name": "Gustavo", "dialpad_user_id": "dp-1"},
    {"id": 2, "name": "NoDialpadPM", "dialpad_user_id": ""},
]

JOBS = [
    {"id": 10, "client_name": "Known Client", "sheet_tab": "to_start", "primary_job_type": "Deck",
     "customer_phone": "555-111-2222", "customer_email": ""},
    {"id": 11, "client_name": "Ambiguous A", "sheet_tab": "to_start", "primary_job_type": "Fence",
     "customer_phone": "555-999-0000", "customer_email": ""},
    {"id": 12, "client_name": "Ambiguous B", "sheet_tab": "in_process", "primary_job_type": "Roof",
     "customer_phone": "555-999-0000", "customer_email": ""},
]


def _call(call_id, phone="555-111-2222", duration=60000, state="connected",
          direction="outbound", contact_type="external"):
    return {
        "call_id": call_id,
        "direction": direction,
        "state": state,
        "duration": duration,
        "date_started": "2026-10-20T14:00:00",
        "contact": {"phone_number": phone, "type": contact_type},
    }


def _transcript(lines=None):
    return {"lines": lines if lines is not None else [
        {"type": "transcript", "user_id": "u1", "content": "Hi there"},
        {"type": "transcript", "contact_id": "c1", "content": "Hello"},
    ]}


def _patched(calls_by_user=None, transcripts_by_id=None, jobs=None, pms=None, cursor=None):
    calls_by_user = calls_by_user or {}
    transcripts_by_id = transcripts_by_id or {}
    jobs = jobs if jobs is not None else JOBS
    pms = pms if pms is not None else PMS

    def fake_list_calls(user_id, start_ms, end_ms, **kwargs):
        return calls_by_user.get(user_id, [])

    def fake_get_transcript(call_id, **kwargs):
        return transcripts_by_id.get(call_id)

    return [
        patch("db.state_store.get_pm_list", return_value=pms),
        patch("db.state_store.get_all_active_jobs", return_value=jobs),
        patch("db.state_store.get_ingest_cursor", return_value=cursor),
        patch("db.state_store.set_ingest_cursor"),
        patch("db.state_store.get_config", return_value=""),
        patch("integrations.dialpad_calls.list_calls", side_effect=fake_list_calls),
        patch("integrations.dialpad_calls.get_transcript", side_effect=fake_get_transcript),
    ]


def _run_with(calls_by_user=None, transcripts_by_id=None, jobs=None, pms=None, dry_run=True, cursor=None,
              upsert_mock=None):
    patchers = _patched(calls_by_user, transcripts_by_id, jobs, pms, cursor)
    if upsert_mock is None:
        patchers.append(patch("db.state_store.upsert_customer_message", return_value=True))
    else:
        patchers.append(patch("db.state_store.upsert_customer_message", upsert_mock))
    for p in patchers:
        p.start()
    try:
        return run(_parse_since("7d"), dry_run=dry_run)
    finally:
        for p in patchers:
            p.stop()


def test_parse_since_formats():
    assert _parse_since("7d") == timedelta(days=7)
    assert _parse_since("12h") == timedelta(hours=12)
    assert _parse_since("30m") == timedelta(minutes=30)


def test_skips_pm_without_dialpad_id_and_logs_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        stats = _run_with(calls_by_user={}, dry_run=True)
    assert stats["pms_skipped_missing_dialpad_id"] == 1
    assert any("NoDialpadPM" in r.message for r in caplog.records)


def test_normal_call_with_transcript_matches_known_phone():
    calls = {"dp-1": [_call(1, phone="555-111-2222")]}
    transcripts = {1: _transcript()}
    stats = _run_with(calls, transcripts, dry_run=True)
    assert stats["calls_seen"] == 1
    assert stats["with_transcript"] == 1
    assert stats["matched"] == 1


def test_internal_call_is_skipped():
    calls = {"dp-1": [_call(2, contact_type="user")]}
    stats = _run_with(calls, {}, dry_run=True)
    assert stats["skipped_internal"] == 1
    assert stats["with_transcript"] == 0


def test_not_connected_call_is_skipped():
    calls = {"dp-1": [_call(3, duration=0, state="connected")]}
    stats = _run_with(calls, {}, dry_run=True)
    assert stats["skipped_not_connected"] == 1


def test_zero_line_transcript_is_empty_and_not_stored():
    calls = {"dp-1": [_call(4)]}
    transcripts = {4: _transcript(lines=[])}
    stats = _run_with(calls, transcripts, dry_run=True)
    assert stats["empty"] == 1
    assert stats["with_transcript"] == 0


def test_not_ready_transcript_treated_same_as_empty():
    # Dialpad's docs don't distinguish "not ready" from "no transcript" -
    # a None return (e.g. a 404) is handled identically to 0 lines.
    calls = {"dp-1": [_call(5)]}
    transcripts = {}  # get_transcript returns None for call_id=5
    stats = _run_with(calls, transcripts, dry_run=True)
    assert stats["empty"] == 1


def test_ambiguous_phone_is_counted_and_never_guesses():
    calls = {"dp-1": [_call(6, phone="555-999-0000")]}
    transcripts = {6: _transcript()}
    stats = _run_with(calls, transcripts, dry_run=True)
    assert stats["ambiguous"] == 1
    assert stats["matched"] == 0


def test_unknown_phone_is_unmatched():
    calls = {"dp-1": [_call(7, phone="555-000-0000")]}
    transcripts = {7: _transcript()}
    stats = _run_with(calls, transcripts, dry_run=True)
    assert stats["unmatched"] == 1


def test_dry_run_never_calls_upsert():
    calls = {"dp-1": [_call(8)]}
    transcripts = {8: _transcript()}
    with patch("db.state_store.upsert_customer_message") as mock_upsert:
        patchers = _patched(calls, transcripts)
        for p in patchers:
            p.start()
        try:
            run(_parse_since("7d"), dry_run=True)
        finally:
            for p in patchers:
                p.stop()
    mock_upsert.assert_not_called()


def test_dry_run_sample_truncates_text_and_masks_phone():
    calls = {"dp-1": [_call(9, phone="555-111-2222")]}
    transcripts = {9: _transcript(lines=[{"type": "transcript", "contact_id": "c1", "content": "x" * 200}])}
    stats = _run_with(calls, transcripts, dry_run=True)
    assert len(stats["samples"]) == 1
    sample = stats["samples"][0]
    assert len(sample["customer"]) <= 80
    assert "1111" not in sample["phone"] and sample["phone"].endswith("2222")
    assert sample["phone"].startswith("***")


def test_real_run_upserts_and_cursor_advances_only_on_success():
    calls = {"dp-1": [_call(10)]}
    transcripts = {10: _transcript()}
    with patch("db.state_store.get_pm_list", return_value=PMS), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor") as mock_set_cursor, \
         patch("db.state_store.get_config", return_value=""), \
         patch("integrations.dialpad_calls.list_calls", side_effect=lambda user_id, *a, **kw: calls.get(user_id, [])), \
         patch("integrations.dialpad_calls.get_transcript", side_effect=lambda call_id, **kw: transcripts.get(call_id)), \
         patch("db.state_store.upsert_customer_message", return_value=True):
        run(_parse_since("7d"), dry_run=False)
    mock_set_cursor.assert_called_once()


def test_cursor_does_not_advance_on_error():
    def raise_error(*a, **kw):
        raise RuntimeError("boom")

    with patch("db.state_store.get_pm_list", return_value=PMS), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor") as mock_set_cursor, \
         patch("db.state_store.get_config", return_value=""), \
         patch("integrations.dialpad_calls.list_calls", side_effect=raise_error):
        try:
            run(_parse_since("7d"), dry_run=False)
        except RuntimeError:
            pass
    mock_set_cursor.assert_not_called()


def test_idempotent_rerun_produces_zero_new_rows_second_time():
    calls = {"dp-1": [_call(11)]}
    transcripts = {11: _transcript()}
    store: dict = {}

    def fake_upsert(row):
        key = (row["source"], row["external_id"])
        inserted = key not in store
        store[key] = row
        return inserted

    stats1 = _run_with(calls, transcripts, dry_run=False, upsert_mock=fake_upsert)
    stats2 = _run_with(calls, transcripts, dry_run=False, upsert_mock=fake_upsert)
    assert stats1["new_rows"] == 1
    assert stats2["new_rows"] == 0


def test_is_enabled_reads_config():
    with patch("db.state_store.get_config", return_value="true"):
        assert is_enabled() is True
    with patch("db.state_store.get_config", return_value="false"):
        assert is_enabled() is False
    with patch("db.state_store.get_config", return_value=None):
        assert is_enabled() is False
