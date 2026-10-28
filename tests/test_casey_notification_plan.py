from datetime import datetime, timezone

from agents.casey.notification_plan import (
    plan_notifications, build_message, DEFAULT_EMAIL_MATRIX,
    apply_backfill_suppression, BACKFILL_REASON,
)

FLAG = {
    "id": 42,
    "severity": "Medium",
    "job_key": "Acme|to_start|Deck",
    "customer_name": "Acme",
    "address": "123 Main St",
    "pm_name": "Gustavo",
    "primary_reason": "Rework required",
    "issue_summary": "Customer unhappy with rework pace.",
    "link": "https://dashboard.example/flags/42",
}


def test_no_op_and_note_improvement_plan_nothing():
    assert plan_notifications(FLAG, "no_op") == []
    assert plan_notifications(FLAG, "note_improvement") == []


def test_create_always_posts_slack_channel_and_pm_dm():
    notifications = plan_notifications(FLAG, "create")
    channels = [n["channel"] for n in notifications]
    assert "slack_channel" in channels
    assert "slack_dm" in channels
    dm = next(n for n in notifications if n["channel"] == "slack_dm")
    assert dm["recipient"] == "Gustavo"


def test_low_severity_emails_only_pm_and_ana_maria():
    flag = {**FLAG, "severity": "Low"}
    notifications = plan_notifications(
        flag, "create",
        recipient_addresses={"ana_maria": "ana@example.com", "tatiana": "tatiana@example.com", "chris": "chris@example.com"},
        pm_email="pm@example.com",
    )
    emails = sorted(n["recipient"] for n in notifications if n["channel"] == "email")
    assert emails == ["ana@example.com", "pm@example.com"]


def test_medium_severity_adds_tatiana():
    flag = {**FLAG, "severity": "Medium"}
    notifications = plan_notifications(
        flag, "create",
        recipient_addresses={"ana_maria": "ana@example.com", "tatiana": "tatiana@example.com", "chris": "chris@example.com"},
        pm_email="pm@example.com",
    )
    emails = sorted(n["recipient"] for n in notifications if n["channel"] == "email")
    assert emails == ["ana@example.com", "pm@example.com", "tatiana@example.com"]


def test_high_and_critical_add_chris():
    for severity in ("High", "Critical"):
        flag = {**FLAG, "severity": severity}
        notifications = plan_notifications(
            flag, "create",
            recipient_addresses={"ana_maria": "ana@example.com", "tatiana": "tatiana@example.com", "chris": "chris@example.com"},
            pm_email="pm@example.com",
        )
        emails = sorted(n["recipient"] for n in notifications if n["channel"] == "email")
        assert emails == ["ana@example.com", "chris@example.com", "pm@example.com", "tatiana@example.com"], severity


def test_unconfigured_empty_recipient_is_skipped():
    flag = {**FLAG, "severity": "Critical"}
    notifications = plan_notifications(
        flag, "create",
        recipient_addresses={"ana_maria": "", "tatiana": "", "chris": ""},
        pm_email="pm@example.com",
    )
    emails = [n["recipient"] for n in notifications if n["channel"] == "email"]
    assert emails == ["pm@example.com"]


def test_override_replaces_every_email_recipient():
    flag = {**FLAG, "severity": "Critical"}
    notifications = plan_notifications(
        flag, "create",
        recipient_addresses={
            "ana_maria": "ana@example.com", "tatiana": "tatiana@example.com", "chris": "chris@example.com",
            "override": "test-inbox@example.com",
        },
        pm_email="pm@example.com",
    )
    emails = [n["recipient"] for n in notifications if n["channel"] == "email"]
    assert emails and all(addr == "test-inbox@example.com" for addr in emails)


def test_dedup_key_is_flag_id_plus_severity():
    notifications = plan_notifications(FLAG, "create")
    assert all(n["dedup_key"] == "42:Medium" for n in notifications)


def test_escalation_produces_a_fresh_dedup_key_for_new_severity():
    escalated_flag = {**FLAG, "severity": "High"}
    notifications = plan_notifications(escalated_flag, "escalate")
    assert all(n["dedup_key"] == "42:High" for n in notifications)


def test_create_separate_also_plans_notifications():
    notifications = plan_notifications(FLAG, "create_separate")
    assert notifications


def test_message_starts_with_severity_label():
    message = build_message(FLAG)
    assert message.startswith("MEDIUM:")
    assert "PM: Gustavo" in message
    assert "Rework required" in message
    assert FLAG["link"] in message


def test_escalation_message_says_so_explicitly():
    escalated_flag = {**FLAG, "severity": "High"}
    message = build_message(escalated_flag, action="escalate", old_severity="Medium")
    assert message.startswith("HIGH:")
    assert "ESCALATED to HIGH from MEDIUM" in message


def test_non_escalation_message_has_no_escalation_line():
    message = build_message(FLAG, action="create")
    assert "ESCALATED" not in message


def test_plan_notifications_passes_old_severity_through_to_message():
    escalated_flag = {**FLAG, "severity": "High"}
    notifications = plan_notifications(escalated_flag, "escalate", old_severity="Medium")
    assert all("ESCALATED to HIGH from MEDIUM" in n["message"] for n in notifications)


def test_default_email_matrix_matches_table():
    assert DEFAULT_EMAIL_MATRIX["Low"] == ["ana_maria"]
    assert DEFAULT_EMAIL_MATRIX["Medium"] == ["ana_maria", "tatiana"]
    assert DEFAULT_EMAIL_MATRIX["High"] == ["ana_maria", "tatiana", "chris"]
    assert DEFAULT_EMAIL_MATRIX["Critical"] == ["ana_maria", "tatiana", "chris"]


# --- Backfill suppression (Stage 7) -----------------------------------------

def test_suppressed_when_live_since_unset():
    rows = plan_notifications(FLAG, "create")
    event_time = datetime(2026, 10, 1, tzinfo=timezone.utc)
    result = apply_backfill_suppression(rows, event_time, live_since=None)
    assert result  # still returns the planned rows, just marked suppressed
    for row in result:
        assert row["status"] == "suppressed"
        assert row["reason"] == BACKFILL_REASON


def test_suppressed_when_event_before_live_since():
    rows = plan_notifications(FLAG, "create")
    live_since = datetime(2026, 11, 1, tzinfo=timezone.utc)
    event_time = datetime(2026, 10, 1, tzinfo=timezone.utc)  # before live_since
    result = apply_backfill_suppression(rows, event_time, live_since)
    for row in result:
        assert row["status"] == "suppressed"
        assert row["reason"] == BACKFILL_REASON


def test_planned_when_event_after_live_since():
    rows = plan_notifications(FLAG, "create")
    live_since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    event_time = datetime(2026, 11, 1, tzinfo=timezone.utc)  # after live_since
    result = apply_backfill_suppression(rows, event_time, live_since)
    for row in result:
        assert row["status"] == "planned"
        assert "reason" not in row or row.get("reason") is None


def test_suppressed_when_event_time_missing():
    rows = plan_notifications(FLAG, "create")
    live_since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    result = apply_backfill_suppression(rows, None, live_since)
    for row in result:
        assert row["status"] == "suppressed"
        assert row["reason"] == BACKFILL_REASON
