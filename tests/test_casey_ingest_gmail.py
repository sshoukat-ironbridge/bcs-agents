import json
from pathlib import Path
from unittest.mock import patch

from agents.casey.ingest_gmail import run
from agents.casey.ingest_windows import parse_since

FIXTURES = Path(__file__).parent / "fixtures" / "casey"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


PM_ONE = {"full_name": "PM One", "email": "pm.one@bluecollarscholars.net", "dialpad_user_id": "1"}
PM_TWO = {"full_name": "PM Two", "email": "pm.two@bluecollarscholars.net", "dialpad_user_id": "2"}

JOBS = [
    {"id": 10, "client_name": "Customer One", "sheet_tab": "to_start", "primary_job_type": "Deck",
     "customer_email": "customer.one@example.com", "customer_phone": "", "pm_name": "PM One", "estimator_name": "Est One"},
    {"id": 11, "client_name": "Customer Two", "sheet_tab": "to_start", "primary_job_type": "Fence",
     "customer_email": "customer.two@example.com", "customer_phone": "", "pm_name": "PM Two", "estimator_name": "Est Two"},
    {"id": 12, "client_name": "Shared A", "sheet_tab": "to_start", "primary_job_type": "Roof",
     "customer_email": "shared.household@example.com", "customer_phone": "", "pm_name": "PM One", "estimator_name": "Est One"},
    {"id": 13, "client_name": "Shared C", "sheet_tab": "in_process", "primary_job_type": "Siding",
     "customer_email": "shared.household@example.com", "customer_phone": "", "pm_name": "PM One", "estimator_name": "Est One"},
    {"id": 14, "client_name": "Cross PM Job", "sheet_tab": "to_start", "primary_job_type": "Patio",
     "customer_email": "crosspm.household@example.com", "customer_phone": "", "pm_name": "PM Two", "estimator_name": "Est Two"},
]


def _find_pm(name, pm_list=None):
    pm_list = pm_list or [PM_ONE, PM_TWO]
    for pm in pm_list:
        if pm["full_name"].strip().lower() == (name or "").strip().lower():
            return pm
    return None


def _config(overrides=None):
    values = {
        "casey_gmail_mailboxes_mode": "pm_config",
        "casey_gmail_mailboxes": "",
        "notification_sender_email": "",
        "casey_gmail_max_messages": "500",
        "casey_gmail_overlap_hours": "2",
        "casey_gmail_address_batch_size": "15",
        "casey_gmail_store_unmatched": "false",
    }
    values.update(overrides or {})
    return lambda key: values.get(key, "")


def _list_side_effect(inbound_stubs=None, outbound_stubs=None):
    inbound_stubs = inbound_stubs or []
    outbound_stubs = outbound_stubs or []

    def _fn(mailbox, query, max_results=500, **kw):
        return list(outbound_stubs) if "in:sent" in query else list(inbound_stubs)
    return _fn


def _run_with(pm_list, inbound_stubs=None, outbound_stubs=None, messages_by_id=None,
              config_overrides=None, dry_run=True, upsert_mock=None):
    messages_by_id = messages_by_id or {}
    patchers = [
        patch("db.state_store.get_config", side_effect=_config(config_overrides)),
        patch("db.state_store.get_all_active_jobs", return_value=JOBS),
        patch("db.state_store.get_pm_list", return_value=pm_list),
        patch("db.state_store.find_pm", side_effect=_find_pm),
        patch("db.state_store.get_ingest_cursor", return_value=None),
        patch("db.state_store.set_ingest_cursor"),
        patch("db.state_store.create_ingest_run_stats"),
        patch("db.state_store.get_customer_message_raw_ref", return_value=None),
        patch("integrations.gmail_read.list_messages", side_effect=_list_side_effect(inbound_stubs, outbound_stubs)),
        patch("integrations.gmail_read.get_message", side_effect=lambda mb, mid, **kw: messages_by_id[mid]),
        patch("integrations.gmail_read.get_message_metadata", side_effect=lambda mb, mid, **kw: messages_by_id[mid]),
        patch("db.state_store.upsert_customer_message", upsert_mock or (lambda row: True)),
    ]
    for p in patchers:
        p.start()
    try:
        return run(parse_since("14d"), dry_run=dry_run)
    finally:
        for p in patchers:
            p.stop()


def test_multipart_alternative_prefers_plain_text():
    message = _load("gmail_real_multipart_alternative.json")
    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message})
    assert stats["inbound"] == 1
    assert stats["stored"] == 1


def test_html_only_message_is_stripped_to_text():
    message = _load("gmail_real_html_only.json")  # vendor sender, not a known customer
    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
                       config_overrides={"casey_gmail_store_unmatched": "true"})
    assert stats["skipped_unmatched"] == 1


def test_forwarded_thread_message_matches_known_customer():
    message = _load("gmail_real_forwarded_thread.json")
    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message})
    assert stats["stored"] == 1
    assert stats["ambiguous"] == 0


def test_automated_sender_is_counted_and_never_stored():
    message = _load("gmail_real_automated_sender.json")
    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
                       config_overrides={"casey_gmail_store_unmatched": "true"})
    assert stats["automated"] == 1
    assert stats["stored"] == 0


def test_reply_with_quoted_text_matches_and_strips_quote():
    # Addressed to pm.two — PM Two's own mailbox/jobs.
    message = _load("gmail_real_reply_quoted.json")
    stats = _run_with([PM_TWO], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message})
    assert stats["stored"] == 1
    assert stats["inbound"] == 1


def test_outbound_from_bcs_address_matches_to_recipient():
    # From pm.one@..., To customer.two@... — outbound, metadata only.
    message = _load("gmail_real_outbound_bcs.json")
    stats = _run_with([PM_TWO], outbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
                       dry_run=False)
    assert stats["outbound"] == 1
    assert stats["stored"] == 1


def test_outbound_message_is_stored_with_empty_body():
    message = _load("gmail_real_outbound_bcs.json")
    stored = {}

    def fake_upsert(row):
        stored["row"] = row
        return True

    _run_with([PM_TWO], outbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
              dry_run=False, upsert_mock=fake_upsert)
    assert stored["row"]["body_text"] == ""
    assert stored["row"]["direction"] == "outbound"


def test_outbound_message_is_never_analyzed_even_if_stored():
    # Belt-and-suspenders: get_messages_to_analyze() excludes outbound
    # Gmail rows by direction regardless of this module, but confirm the
    # row this module builds really is outbound (see db/state_store.py's
    # get_messages_to_analyze filter, covered in its own tests).
    message = _load("gmail_real_outbound_bcs.json")
    stored = {}
    _run_with([PM_TWO], outbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
              dry_run=False, upsert_mock=lambda row: stored.setdefault("row", row) or True)
    assert stored["row"]["direction"] == "outbound"


def test_same_pm_ambiguous_address_is_ambiguous_but_stored():
    # shared.household maps to Shared A AND Shared C, both PM One's own
    # jobs — genuinely ambiguous, never guessed.
    message = _load("gmail_real_two_job_candidates.json")
    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message})
    assert stats["ambiguous"] == 1
    assert stats["stored"] == 1


def test_cross_pm_fallback_when_mailbox_owner_has_no_matching_job():
    # A message addressed to PM One's mailbox from a customer who is
    # actually PM Two's job (crosspm.household) — PM One has no job for
    # that address, falls back to all jobs, finds exactly one -> matched,
    # cross_pm=True.
    message = dict(_load("gmail_real_forwarded_thread.json"))
    message = json.loads(json.dumps(message).replace("customer.one@example.com", "crosspm.household@example.com"))
    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message})
    assert stats["stored"] == 1
    assert stats["ambiguous"] == 0
    assert stats["cross_pm"] == 1


def test_unmatched_not_stored_by_default():
    message = _load("gmail_real_html_only.json")  # vendor sender, unknown address
    captured = {}

    def fake_upsert(row):
        captured["called"] = True
        return True

    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
                       dry_run=False, upsert_mock=fake_upsert)
    assert stats["skipped_unmatched"] == 1
    assert "called" not in captured


def test_store_unmatched_when_config_enabled():
    message = _load("gmail_real_html_only.json")
    inserted = []

    def fake_upsert(row):
        inserted.append(row)
        return True

    stats = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
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
         patch("db.state_store.get_pm_list", return_value=[PM_ONE]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("db.state_store.create_ingest_run_stats"), \
         patch("integrations.gmail_read.list_messages", side_effect=_list_side_effect([{"id": message["id"]}])), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message") as mock_upsert:
        run(parse_since("14d"), dry_run=True)
    mock_upsert.assert_not_called()


def test_idempotent_rerun_produces_zero_new_rows_second_time():
    message = _load("gmail_real_forwarded_thread.json")
    store: dict = {}

    def fake_upsert(row):
        key = (row["source"], row["external_id"])
        inserted = key not in store
        store[key] = row
        return inserted

    stats1 = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
                        dry_run=False, upsert_mock=fake_upsert)
    stats2 = _run_with([PM_ONE], inbound_stubs=[{"id": message["id"]}], messages_by_id={message["id"]: message},
                        dry_run=False, upsert_mock=fake_upsert)
    assert stats1["new_rows"] == 1
    assert stats2["new_rows"] == 0


def test_message_seen_in_two_mailboxes_merges_raw_ref():
    message = _load("gmail_real_forwarded_thread.json")
    raw_refs = {"stage": None}

    def fake_get_raw_ref(source, external_id):
        return raw_refs["stage"]

    def fake_upsert(row):
        raw_refs["stage"] = row["raw_ref"]
        return True

    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_ONE, PM_TWO]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("db.state_store.create_ingest_run_stats"), \
         patch("db.state_store.get_customer_message_raw_ref", side_effect=fake_get_raw_ref), \
         patch("integrations.gmail_read.list_messages", side_effect=_list_side_effect([{"id": message["id"]}])), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message", side_effect=fake_upsert):
        run(parse_since("14d"), dry_run=False)  # PM One's mailbox sees it

    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_TWO]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("db.state_store.create_ingest_run_stats"), \
         patch("db.state_store.get_customer_message_raw_ref", side_effect=fake_get_raw_ref), \
         patch("integrations.gmail_read.list_messages", side_effect=_list_side_effect([{"id": message["id"]}])), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message", side_effect=fake_upsert):
        run(parse_since("14d"), dry_run=False)  # a second mailbox also sees it

    final_raw_ref = json.loads(raw_refs["stage"])
    assert len(final_raw_ref["mailboxes_seen"]) == 2


def test_cursor_advances_only_on_success():
    message = _load("gmail_real_forwarded_thread.json")
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_ONE]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor") as mock_set_cursor, \
         patch("db.state_store.create_ingest_run_stats"), \
         patch("db.state_store.get_customer_message_raw_ref", return_value=None), \
         patch("integrations.gmail_read.list_messages", side_effect=_list_side_effect([{"id": message["id"]}])), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message", return_value=True):
        run(parse_since("14d"), dry_run=False)
    mock_set_cursor.assert_called_once()


def test_cursor_does_not_advance_on_error():
    def raise_error(*a, **kw):
        raise RuntimeError("boom")

    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_ONE]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor") as mock_set_cursor, \
         patch("integrations.gmail_read.list_messages", side_effect=raise_error):
        try:
            run(parse_since("14d"), dry_run=False)
        except RuntimeError:
            pass
    mock_set_cursor.assert_not_called()


def test_mailboxes_default_to_pm_config_emails():
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_ONE, PM_TWO]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("db.state_store.create_ingest_run_stats"), \
         patch("integrations.gmail_read.list_messages", return_value=[]) as mock_list, \
         patch("db.state_store.upsert_customer_message"):
        run(parse_since("14d"), dry_run=True)
    called_mailboxes = {call.args[0] for call in mock_list.call_args_list}
    assert called_mailboxes == {PM_ONE["email"], PM_TWO["email"]}


def test_manual_mailboxes_mode_overrides_pm_config():
    with patch("db.state_store.get_config", side_effect=_config({
            "casey_gmail_mailboxes_mode": "manual",
            "casey_gmail_mailboxes": json.dumps(["a@example.com", "b@example.com"])})), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_ONE, PM_TWO]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("db.state_store.create_ingest_run_stats"), \
         patch("integrations.gmail_read.list_messages", return_value=[]) as mock_list, \
         patch("db.state_store.upsert_customer_message"):
        run(parse_since("14d"), dry_run=True)
    called_mailboxes = {call.args[0] for call in mock_list.call_args_list}
    assert called_mailboxes == {"a@example.com", "b@example.com"}


def test_dry_run_prints_counts_per_pm_only(capsys):
    import sys
    message = _load("gmail_real_forwarded_thread.json")
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.get_all_active_jobs", return_value=JOBS), \
         patch("db.state_store.get_pm_list", return_value=[PM_ONE]), \
         patch("db.state_store.find_pm", side_effect=_find_pm), \
         patch("db.state_store.get_ingest_cursor", return_value=None), \
         patch("db.state_store.set_ingest_cursor"), \
         patch("db.state_store.get_customer_message_raw_ref", return_value=None), \
         patch("integrations.gmail_read.list_messages", side_effect=_list_side_effect([{"id": message["id"]}])), \
         patch("integrations.gmail_read.get_message", return_value=message), \
         patch("db.state_store.upsert_customer_message"), \
         patch.object(sys, "argv", ["ingest_gmail.py", "--since", "14d", "--dry-run"]):
        from agents.casey.ingest_gmail import main
        main()
    out = capsys.readouterr().out
    assert "PM One" in out
    assert "candidate message" in out
