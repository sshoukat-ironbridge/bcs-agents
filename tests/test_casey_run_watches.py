from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.casey.run_watches import run

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def _config(overrides=None):
    values = {
        "casey_response_window_hours": "24",
        "casey_sms_counts_as_response": "false",
        "casey_email_ana_maria": "", "casey_email_tatiana": "", "casey_email_chris": "",
        "casey_email_override": "",
    }
    values.update(overrides or {})
    return lambda key: values.get(key, "")


def _base_patches(unwatched=None, open_watches=None, candidates=None, find_pm_result=None):
    return [
        patch("db.state_store.get_config", side_effect=_config()),
        patch("db.state_store.find_unwatched_inbound_messages", return_value=unwatched or []),
        patch("db.state_store.create_response_watch", return_value=1),
        patch("db.state_store.get_open_response_watches", return_value=open_watches or []),
        patch("db.state_store.get_response_candidates", return_value=candidates or []),
        patch("db.state_store.update_response_watch"),
        patch("db.state_store.notification_exists", return_value=False),
        patch("db.state_store.create_notification"),
        patch("db.state_store.find_pm", return_value=find_pm_result),
    ]


def _run_with(patchers, dry_run=False):
    for p in patchers:
        p.start()
    try:
        return run(dry_run=dry_run)
    finally:
        for p in patchers:
            p.stop()


def test_creates_a_watch_for_unwatched_inbound_message():
    unwatched = [{"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck",
                  "pm_name": "PM One", "customer_email": "c@example.com",
                  "customer_phone": "", "occurred_at": T0}]
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=unwatched), \
         patch("db.state_store.create_response_watch") as mock_create, \
         patch("db.state_store.get_open_response_watches", return_value=[]):
        mock_create.return_value = 1
        stats = run(dry_run=False)
    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args[0][0]
    assert call_kwargs["due_at"] == T0 + timedelta(hours=24)
    assert stats["created"] == 1


def test_newly_created_watch_is_evaluated_exactly_once_not_twice():
    # Regression: a real (non-dry) run persists new watches via
    # create_response_watch() BEFORE evaluating, so get_open_response_watches()
    # already includes them. Adding the in-memory new_watches list on top
    # would double-count every newly created watch's classification.
    unwatched = [{"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck",
                  "pm_name": "PM One", "customer_email": "c@example.com",
                  "customer_phone": "", "occurred_at": T0}]
    persisted_watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck",
                        "pm_name": "PM One", "customer_email": "c@example.com",
                        "received_at": T0, "due_at": T0 + timedelta(hours=24), "status": "waiting"}
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=unwatched), \
         patch("db.state_store.create_response_watch", return_value=1), \
         patch("db.state_store.get_open_response_watches", return_value=[persisted_watch]), \
         patch("db.state_store.get_response_candidates", return_value=[]), \
         patch("agents.casey.run_watches.datetime") as mock_dt:
        mock_dt.now.return_value = T0 + timedelta(hours=1)
        stats = run(dry_run=False)
    assert stats["waiting"] == 1  # not 2


def test_dry_run_never_creates_watches():
    unwatched = [{"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck",
                  "pm_name": "PM One", "customer_email": "c@example.com",
                  "customer_phone": "", "occurred_at": T0}]
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=unwatched), \
         patch("db.state_store.create_response_watch") as mock_create, \
         patch("db.state_store.get_open_response_watches", return_value=[]):
        stats = run(dry_run=True)
    mock_create.assert_not_called()
    assert stats["created"] == 0


def test_waiting_watch_within_window_stays_waiting():
    now = T0 + timedelta(hours=1)
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "waiting"}
    patchers = _base_patches(open_watches=[watch])
    with patch("agents.casey.run_watches.datetime") as mock_dt:
        mock_dt.now.return_value = now
        stats = _run_with(patchers)
    assert stats["waiting"] == 1
    assert stats["responded"] == 0
    assert stats["overdue"] == 0


def test_watch_with_outbound_response_in_thread_becomes_responded():
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "waiting"}
    candidates = [{"source": "gmail", "direction": "outbound", "thread_id": "t1",
                   "occurred_at": T0 + timedelta(hours=2), "message_id": 99}]
    patchers = _base_patches(open_watches=[watch], candidates=candidates)
    for p in patchers:
        p.start()
    try:
        with patch("db.state_store.update_response_watch") as mock_update:
            stats = run(dry_run=False)
    finally:
        for p in patchers:
            p.stop()
    assert stats["responded"] == 1
    mock_update.assert_called_once()
    assert mock_update.call_args[0][1] == "responded"


def test_watch_past_due_with_no_response_becomes_overdue_and_plans_notification():
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "waiting"}
    patchers = [
        patch("db.state_store.get_config", side_effect=_config()),
        patch("db.state_store.find_unwatched_inbound_messages", return_value=[]),
        patch("db.state_store.get_open_response_watches", return_value=[watch]),
        patch("db.state_store.get_response_candidates", return_value=[]),
        patch("db.state_store.notification_exists", return_value=False),
        patch("db.state_store.find_pm", return_value=None),
    ]
    for p in patchers:
        p.start()
    try:
        with patch("agents.casey.run_watches.datetime") as mock_dt, \
             patch("db.state_store.update_response_watch") as mock_update, \
             patch("db.state_store.create_notification") as mock_notify:
            mock_dt.now.return_value = T0 + timedelta(hours=30)
            stats = run(dry_run=False)
    finally:
        for p in patchers:
            p.stop()
    assert stats["overdue"] == 1
    mock_update.assert_called_once_with(1, "overdue")
    assert mock_notify.called


def test_overdue_notification_only_planned_once_per_watch():
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "overdue"}  # already overdue - classify will say overdue again, status unchanged -> no replan attempt needed but notification_exists guards it anyway
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=[]), \
         patch("db.state_store.get_open_response_watches", return_value=[watch]), \
         patch("db.state_store.get_response_candidates", return_value=[]), \
         patch("db.state_store.notification_exists", return_value=True), \
         patch("db.state_store.create_notification") as mock_notify, \
         patch("db.state_store.update_response_watch"):
        run(dry_run=False)
    mock_notify.assert_not_called()


def test_dry_run_still_classifies_newly_discovered_inbound_messages():
    # Even with nothing persisted yet, a dry run should still show
    # meaningful counts for messages that would become new watches.
    unwatched = [{"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck",
                  "pm_name": "PM One", "customer_email": "c@example.com",
                  "customer_phone": "", "occurred_at": T0}]
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=unwatched), \
         patch("db.state_store.create_response_watch") as mock_create, \
         patch("db.state_store.get_open_response_watches", return_value=[]), \
         patch("db.state_store.get_response_candidates", return_value=[]), \
         patch("agents.casey.run_watches.datetime") as mock_dt:
        mock_dt.now.return_value = T0 + timedelta(hours=1)
        stats = run(dry_run=True)
    mock_create.assert_not_called()
    assert stats["created"] == 0
    assert stats["waiting"] == 1


def test_dry_run_never_updates_or_notifies():
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "waiting"}
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=[]), \
         patch("db.state_store.get_open_response_watches", return_value=[watch]), \
         patch("db.state_store.get_response_candidates", return_value=[]), \
         patch("db.state_store.notification_exists", return_value=False), \
         patch("agents.casey.run_watches.datetime") as mock_dt, \
         patch("db.state_store.update_response_watch") as mock_update, \
         patch("db.state_store.create_notification") as mock_notify:
        mock_dt.now.return_value = T0 + timedelta(hours=30)
        stats = run(dry_run=True)
    assert stats["overdue"] == 1
    mock_update.assert_not_called()
    mock_notify.assert_not_called()


def test_find_unwatched_inbound_messages_is_scoped_to_gmail_source():
    # Regression: this function must never pick up inbound dialpad_call
    # rows — the 24h response-watch rule is specifically about customer
    # emails. Caught live: 82 of 88 watches had come from dialpad_call
    # rows before this filter was added.
    import inspect
    import db.state_store as state_store_module
    source = inspect.getsource(state_store_module.find_unwatched_inbound_messages)
    assert 'CustomerMessage.source == "gmail"' in source


def test_overdue_notification_suppressed_when_live_since_unset():
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "waiting"}
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=[]), \
         patch("db.state_store.get_open_response_watches", return_value=[watch]), \
         patch("db.state_store.get_response_candidates", return_value=[]), \
         patch("db.state_store.notification_exists", return_value=False), \
         patch("db.state_store.find_pm", return_value=None), \
         patch("agents.casey.run_watches.datetime") as mock_dt, \
         patch("db.state_store.update_response_watch"), \
         patch("db.state_store.create_notification") as mock_notify:
        mock_dt.now.return_value = T0 + timedelta(hours=30)
        run(dry_run=False)
    stored_rows = [call.args[0] for call in mock_notify.call_args_list]
    assert stored_rows
    for row in stored_rows:
        assert row["status"] == "suppressed"
        assert row["reason"] == "backfill"


def test_overdue_notification_planned_when_live_since_is_in_the_past():
    watch = {"id": 1, "thread_id": "t1", "job_key": "A|to_start|Deck", "pm_name": "PM One",
             "customer_email": "c@example.com", "received_at": T0, "due_at": T0 + timedelta(hours=24),
             "status": "waiting"}
    with patch("db.state_store.get_config", side_effect=_config({"casey_live_since": "2020-01-01T00:00:00+00:00"})), \
         patch("db.state_store.find_unwatched_inbound_messages", return_value=[]), \
         patch("db.state_store.get_open_response_watches", return_value=[watch]), \
         patch("db.state_store.get_response_candidates", return_value=[]), \
         patch("db.state_store.notification_exists", return_value=False), \
         patch("db.state_store.find_pm", return_value=None), \
         patch("agents.casey.run_watches.datetime") as mock_dt, \
         patch("db.state_store.update_response_watch"), \
         patch("db.state_store.create_notification") as mock_notify:
        mock_dt.now.return_value = T0 + timedelta(hours=30)
        run(dry_run=False)
    stored_rows = [call.args[0] for call in mock_notify.call_args_list]
    assert stored_rows
    for row in stored_rows:
        assert row["status"] == "planned"
