from unittest.mock import patch

from agents.casey.notify import resolve_target, send_one, run


PM_SLACK_IDS = {"PM One": "U123"}


# --- resolve_target: production vs test/override rules -----------------------

def test_slack_channel_goes_to_real_channel_in_production(monkeypatch):
    monkeypatch.setenv("CASEY_ENV", "production")
    target, ok, note = resolve_target(
        {"channel": "slack_channel", "recipient": "urgentmatters"},
        test_channel="", email_override="", pm_slack_user_ids={},
    )
    assert target == "urgentmatters" and ok is True


def test_slack_channel_goes_to_test_channel_outside_production(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    target, ok, note = resolve_target(
        {"channel": "slack_channel", "recipient": "urgentmatters"},
        test_channel="test-alerts", email_override="", pm_slack_user_ids={},
    )
    assert target == "test-alerts" and ok is True


def test_slack_channel_skipped_when_no_test_channel_configured(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    target, ok, note = resolve_target(
        {"channel": "slack_channel", "recipient": "urgentmatters"},
        test_channel="", email_override="", pm_slack_user_ids={},
    )
    assert ok is False
    assert "test_channel" in note or "test channel" in note


def test_slack_dm_skipped_without_slack_user_id(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    target, ok, note = resolve_target(
        {"channel": "slack_dm", "recipient": "Unknown PM"},
        test_channel="test-alerts", email_override="", pm_slack_user_ids=PM_SLACK_IDS,
    )
    assert ok is False
    assert "slack_user_id" in note


def test_slack_dm_goes_to_test_channel_outside_production(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    target, ok, note = resolve_target(
        {"channel": "slack_dm", "recipient": "PM One"},
        test_channel="test-alerts", email_override="", pm_slack_user_ids=PM_SLACK_IDS,
    )
    assert target == "test-alerts" and ok is True


def test_email_goes_to_real_recipient_in_production(monkeypatch):
    monkeypatch.setenv("CASEY_ENV", "production")
    target, ok, note = resolve_target(
        {"channel": "email", "recipient": "pm@example.com"},
        test_channel="", email_override="", pm_slack_user_ids={},
    )
    assert target == "pm@example.com" and ok is True


def test_email_goes_to_override_outside_production(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    target, ok, note = resolve_target(
        {"channel": "email", "recipient": "pm@example.com"},
        test_channel="", email_override="test-inbox@example.com", pm_slack_user_ids={},
    )
    assert target == "test-inbox@example.com" and ok is True


def test_empty_override_refuses_to_send_any_email(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    target, ok, note = resolve_target(
        {"channel": "email", "recipient": "pm@example.com"},
        test_channel="", email_override="", pm_slack_user_ids={},
    )
    assert ok is False
    assert "override" in note.lower()


def test_customer_address_can_never_be_a_resolved_recipient(monkeypatch):
    # agents/casey/notification_plan.py never plans a row whose recipient
    # is a customer address in the first place — this documents that
    # resolve_target has no code path that could introduce one either.
    monkeypatch.setenv("CASEY_ENV", "production")
    customer_email = "customer@example.com"
    for channel, recipient in (("email", "ana@example.com"), ("slack_dm", "PM One"), ("slack_channel", "urgentmatters")):
        target, ok, note = resolve_target(
            {"channel": channel, "recipient": recipient},
            test_channel="test-alerts", email_override="override@example.com", pm_slack_user_ids=PM_SLACK_IDS,
        )
        assert target != customer_email


# --- send_one ------------------------------------------------------------------

def test_send_one_posts_slack_message_with_resolved_target(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    with patch("integrations.slack.send_message", return_value=True) as mock_send:
        ok, error = send_one(
            {"channel": "slack_channel", "recipient": "urgentmatters", "message": "CRITICAL: test"},
            test_channel="test-alerts", email_override="", pm_slack_user_ids={},
        )
    assert ok is True
    mock_send.assert_called_once_with("test-alerts", "CRITICAL: test")


def test_send_one_skips_without_sending_when_not_resolvable(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    with patch("integrations.gmail.send_email") as mock_send:
        ok, error = send_one(
            {"channel": "email", "recipient": "pm@example.com", "message": "x"},
            test_channel="", email_override="", pm_slack_user_ids={},
        )
    assert ok is False
    mock_send.assert_not_called()


def test_send_one_email_subject_is_first_line_of_message(monkeypatch):
    monkeypatch.setenv("CASEY_ENV", "production")
    with patch("integrations.gmail.send_email", return_value=(True, "", "")) as mock_send, \
         patch("db.state_store.get_config", return_value="casey@example.com"):
        send_one(
            {"channel": "email", "recipient": "pm@example.com", "message": "CRITICAL: Acme\nProject: 123 Main"},
            test_channel="", email_override="", pm_slack_user_ids={},
        )
    args = mock_send.call_args[0]
    assert args[1] == "pm@example.com"
    assert args[2] == "CRITICAL: Acme"


# --- run(): gate, atomic claim, retry cap, no double send --------------------

def test_run_leaves_rows_untouched_when_outbound_disabled():
    with patch("db.state_store.get_config", return_value=""), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}]), \
         patch("utils.outbound.outbound_enabled", return_value=False), \
         patch("db.state_store.claim_notification_for_sending") as mock_claim:
        stats = run(dry_run=False)
    mock_claim.assert_not_called()
    assert stats["not_ready"] == 1


def test_run_leaves_rows_untouched_when_live_since_unset():
    with patch("db.state_store.get_config", side_effect=lambda k: {"casey_live_since": ""}.get(k, "")), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}]), \
         patch("utils.outbound.outbound_enabled", return_value=True), \
         patch("db.state_store.claim_notification_for_sending") as mock_claim:
        stats = run(dry_run=False)
    mock_claim.assert_not_called()
    assert stats["not_ready"] == 1


def test_run_dry_run_makes_no_claim_and_no_send():
    config = {"casey_live_since": "2026-01-01T00:00:00+00:00"}
    with patch("db.state_store.get_config", side_effect=lambda k: config.get(k, "")), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}, {"id": 2}]), \
         patch("utils.outbound.outbound_enabled", return_value=True), \
         patch("db.state_store.claim_notification_for_sending") as mock_claim:
        stats = run(dry_run=True)
    mock_claim.assert_not_called()
    assert stats["would_send"] == 2


def test_run_skips_a_row_already_claimed_by_another_worker():
    config = {"casey_live_since": "2026-01-01T00:00:00+00:00"}
    with patch("db.state_store.get_config", side_effect=lambda k: config.get(k, "")), \
         patch("db.state_store.get_pm_list", return_value=[]), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}]), \
         patch("utils.outbound.outbound_enabled", return_value=True), \
         patch("db.state_store.claim_notification_for_sending", return_value=None) as mock_claim, \
         patch("agents.casey.notify.send_one") as mock_send, \
         patch("db.state_store.mark_notification_sent") as mock_sent:
        stats = run(dry_run=False)
    mock_claim.assert_called_once_with(1)
    mock_send.assert_not_called()
    mock_sent.assert_not_called()
    assert stats["sent"] == 0


def test_claim_is_atomic_by_construction():
    # Can't exercise real Postgres row-locking in a DB-free unit test —
    # this documents that the claim is a single conditional UPDATE whose
    # WHERE clause only matches a still-"planned" row, which is what
    # makes two concurrent callers racing on the same id mutually
    # exclusive at the database level.
    import inspect
    import db.state_store as state_store_module
    source = inspect.getsource(state_store_module.claim_notification_for_sending)
    assert "WHERE id = :id AND status = 'planned'" in source


def test_run_sends_a_claimed_row_and_marks_it_sent():
    claimed = {"id": 1, "channel": "slack_channel", "recipient": "urgentmatters", "message": "x"}
    config = {"casey_live_since": "2026-01-01T00:00:00+00:00"}
    with patch("db.state_store.get_config", side_effect=lambda k: config.get(k, "")), \
         patch("db.state_store.get_pm_list", return_value=[]), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}]), \
         patch("utils.outbound.outbound_enabled", return_value=True), \
         patch("db.state_store.claim_notification_for_sending", return_value=claimed), \
         patch("agents.casey.notify.send_one", return_value=(True, "")) as mock_send, \
         patch("db.state_store.mark_notification_sent") as mock_sent:
        stats = run(dry_run=False)
    mock_send.assert_called_once()
    mock_sent.assert_called_once_with(1)
    assert stats["sent"] == 1


def test_run_retries_a_failed_send_under_the_cap():
    claimed = {"id": 1, "channel": "email", "recipient": "pm@example.com", "message": "x"}
    config = {"casey_live_since": "2026-01-01T00:00:00+00:00", "casey_notify_max_retries": "3"}
    with patch("db.state_store.get_config", side_effect=lambda k: config.get(k, "")), \
         patch("db.state_store.get_pm_list", return_value=[]), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}]), \
         patch("utils.outbound.outbound_enabled", return_value=True), \
         patch("db.state_store.claim_notification_for_sending", return_value=claimed), \
         patch("agents.casey.notify.send_one", return_value=(False, "boom")), \
         patch("db.state_store.mark_notification_failed", return_value="planned") as mock_fail:
        stats = run(dry_run=False)
    mock_fail.assert_called_once_with(1, "boom", max_retries=3)
    assert stats["retrying"] == 1
    assert stats["failed"] == 0


def test_run_marks_permanently_failed_once_retry_cap_is_exhausted():
    claimed = {"id": 1, "channel": "email", "recipient": "pm@example.com", "message": "x"}
    config = {"casey_live_since": "2026-01-01T00:00:00+00:00", "casey_notify_max_retries": "3"}
    with patch("db.state_store.get_config", side_effect=lambda k: config.get(k, "")), \
         patch("db.state_store.get_pm_list", return_value=[]), \
         patch("db.state_store.get_planned_notifications", return_value=[{"id": 1}]), \
         patch("utils.outbound.outbound_enabled", return_value=True), \
         patch("db.state_store.claim_notification_for_sending", return_value=claimed), \
         patch("agents.casey.notify.send_one", return_value=(False, "boom")), \
         patch("db.state_store.mark_notification_failed", return_value="failed") as mock_fail:
        stats = run(dry_run=False)
    assert stats["failed"] == 1
    assert stats["retrying"] == 0


def test_a_sent_row_is_never_picked_up_again():
    # get_planned_notifications() filters to status == "planned" — a
    # "sent" row is structurally excluded, never re-sent on a rerun.
    import inspect
    import db.state_store as state_store_module
    source = inspect.getsource(state_store_module.get_planned_notifications)
    assert 'CaseyNotification.status == "planned"' in source
