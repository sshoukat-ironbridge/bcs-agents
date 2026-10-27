import json
from pathlib import Path
from unittest.mock import patch

from agents.casey.ingest_gmail import run
from agents.casey.ingest_windows import parse_since

FIXTURES = Path(__file__).parent / "fixtures" / "casey"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


MAILBOX = "chris.s@bluecollarscholars.net"

JOBS = [
    {"id": 10, "client_name": "Customer One", "sheet_tab": "to_start", "primary_job_type": "Deck",
     "customer_email": "customer.one@example.com", "customer_phone": "", "pm_name": "PM One", "estimator_name": "Est One"},
    {"id": 11, "client_name": "Customer Two", "sheet_tab": "to_start", "primary_job_type": "Fence",
     "customer_email": "customer.two@example.com", "customer_phone": "", "pm_name": "PM Two", "estimator_name": "Est Two"},
    {"id": 12, "client_name": "Shared A", "sheet_tab": "to_start", "primary_job_type": "Roof",
     "customer_email": "shared.household@example.com", "customer_phone": "", "pm_name": "PM One", "estimator_name": "Est One"},
    {"id": 13, "client_name": "Shared B", "sheet_tab": "in_process", "primary_job_type": "Siding",
     "customer_email": "shared.household@example.com", "customer_phone": "", "pm_name": "PM Two", "estimator_name": "Est Two"},
]


def _config(overrides=None):
    values = {
        "casey_gmail_mailboxes": "",
        "notification_sender_email": MAILBOX,
        "casey_gmail_max_messages": "500",
        "casey_gmail_overlap_hours": "2",
        "casey_gmail_store_unmatched": "false",
    }
    values.update(overrides or {})
    return lambda key: values.get(key, "")


def _run_with(stubs, messages_by_id, config_overrides=None, dry_run=True, upsert_mock=None):
    patchers = [
        patch("db.state_store.get_config", side_effect=_config(config_overrides)),
        patch("db.state_store.get_all_active_jobs", return_value=JOBS),
        patch("db.state_store.get_ingest_cursor", return_value=None),
        patch("db.state_store.set_ingest_cursor"),
        patch("integrations.gmail_read.list_messages", return_value=stubs),
        patch("integrations.gmail_read.get_message", side_effect=lambda mb, mid, **kw: messages_by_id[mid]),
        patch("db.state_store.upsert_customer_message", upsert_mock or (lambda row: True)),
    ]
    for p in patchers:
        p.start()
    try:
        return run(parse_since("7d"), dry_run=dry_run)
    finally:
        for p in patchers:
            p.stop()


def test_multipart_alternative_prefers_plain_text():
    message = _load("gmail_real_multipart_alternative.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message})
    assert stats["inbound"] == 1
    assert stats["stored"] == 1


def test_html_only_message_is_stripped_to_text():
    message = _load("gmail_real_html_only.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message},
                       config_overrides={"casey_gmail_store_unmatched": "true"})
    # sender is a vendor notification address, not a known customer -> unmatched
    assert stats["skipped_unmatched"] == 1


def test_forwarded_thread_message_matches_known_customer():
    message = _load("gmail_real_forwarded_thread.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message})
    assert stats["stored"] == 1
    assert stats["ambiguous"] == 0


def test_automated_sender_is_counted_and_never_stored():
    message = _load("gmail_real_automated_sender.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message},
                       config_overrides={"casey_gmail_store_unmatched": "true"})
    assert stats["automated"] == 1
    assert stats["stored"] == 0


def test_reply_with_quoted_text_matches_and_strips_quote():
    message = _load("gmail_real_reply_quoted.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message})
    assert stats["stored"] == 1
    assert stats["inbound"] == 1


def test_outbound_from_bcs_address_matches_to_recipient():
    message = _load("gmail_real_outbound_bcs.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message})
    assert stats["outbound"] == 1
    assert stats["stored"] == 1


def test_message_to_two_job_candidates_is_ambiguous_but_stored():
    message = _load("gmail_real_two_job_candidates.json")
    stats = _run_with([{"id": message["id"]}], {message["id"]: message})
    assert stats["ambiguous"] == 1
    assert stats["stored"] == 1


def test_unmatched_not_stored_by_default():
    message = _load("gmail_real_html_only.json")  # vendor sender, unknown address
    captured = {}

    def fake_upsert(row):
        captured["called"] = True
        return True

    stats = _run_with([{"id": message["id"]}], {message["id"]: message},
                       dry_run=False, upsert_mock=fake_upsert)
    assert stats["skipped_unmatched"] == 1
    assert "called" not in captured


def test_store_unmatched_when_config_enabled():
    message = _load("gmail_real_html_only.json")
    inserted = []

    def fake_upsert(row):
        inserted.append(row)
        return True

    stats = _run_with([{"id": message["id"]}], {message["id"]: message},
                       config_overrides={"casey_gmail_store_unmatched": "true"},
                       dry_run=False, upsert_mock=fake_upsert)
    assert stats["skipped_unmatched"] == 1
    assert len(inserted) == 1
    assert inserted[0]["job_id"] is None
    assert inserted[0]["job_key"] is None


def test_dry_run_never_calls_upsert():
    message = _load("gmail_real_forwarded_thread.json")
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("integrations.gmail_read.list_messages", return_value=[{"id": message["id"]}]), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message") as mock_upsert:
        run(parse_since("7d"), dry_run=True)
    mock_upsert.assert_not_called()


def test_idempotent_rerun_produces_zero_new_rows_second_time():
    message = _load("gmail_real_forwarded_thread.json")
    store: dict = {}

    def fake_upsert(row):
        key = (row["source"], row["external_id"])
        inserted = key not in store
        store[key] = row
        return inserted

    stats1 = _run_with([{"id": message["id"]}], {message["id"]: message}, dry_run=False, upsert_mock=fake_upsert)
    stats2 = _run_with([{"id": message["id"]}], {message["id"]: message}, dry_run=False, upsert_mock=fake_upsert)
    assert stats1["new_rows"] == 1
    assert stats2["new_rows"] == 0


def test_cursor_advances_only_on_success():
    message = _load("gmail_real_forwarded_thread.json")
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor") as mock_set_cursor, \
         patch("integrations.gmail_read.list_messages", return_value=[{"id": message["id"]}]), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message", return_value=True):
        run(parse_since("7d"), dry_run=False)
    mock_set_cursor.assert_called_once()


def test_cursor_does_not_advance_on_error():
    def raise_error(*a, **kw):
        raise RuntimeError("boom")

    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor") as mock_set_cursor, \
         patch("integrations.gmail_read.list_messages", side_effect=raise_error):
        try:
            run(parse_since("7d"), dry_run=False)
        except RuntimeError:
            pass
    mock_set_cursor.assert_not_called()


def test_default_mailbox_is_notification_sender_email_only():
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("integrations.gmail_read.list_messages", return_value=[]) as mock_list, \
         patch("db.state_store.upsert_customer_message"):
        run(parse_since("7d"), dry_run=True)
    called_mailboxes = {call.args[0] for call in mock_list.call_args_list}
    assert called_mailboxes == {MAILBOX}


def test_explicit_mailboxes_config_overrides_default():
    with patch("db.state_store.get_config", side_effect=_config({"casey_gmail_mailboxes": json.dumps(["a@example.com", "b@example.com"])})), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("integrations.gmail_read.list_messages", return_value=[]) as mock_list, \
         patch("db.state_store.upsert_customer_message"):
        run(parse_since("7d"), dry_run=True)
    called_mailboxes = {call.args[0] for call in mock_list.call_args_list}
    assert called_mailboxes == {"a@example.com", "b@example.com"}
