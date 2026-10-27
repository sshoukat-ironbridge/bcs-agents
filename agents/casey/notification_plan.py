"""Plans (but never sends) Casey's notifications for a flag.

Pure function, no DB/network access — the caller resolves config values
(recipient addresses, the email matrix) and passes them in. Any future
send must go through utils/outbound.outbound_enabled(); this module only
plans rows shaped like casey_notifications inserts.
"""
from datetime import datetime
from typing import Any

from agents.casey.flag_logic import ACTIONS

BACKFILL_REASON = "backfill"

# Severity -> which named recipients get an email, in addition to the PM
# (always included). Doc text says Tatiana joins from High; the doc's
# table says from Medium — defaulted to the table (Medium+), configurable
# via the email_matrix parameter. See docs/casey_foundation.md.
DEFAULT_EMAIL_MATRIX: dict[str, list[str]] = {
    "Low": ["ana_maria"],
    "Medium": ["ana_maria", "tatiana"],
    "High": ["ana_maria", "tatiana", "chris"],
    "Critical": ["ana_maria", "tatiana", "chris"],
}

# app_config keys holding each named recipient's address — EMPTY by default.
# Never hardcode a real address here or in tests.
RECIPIENT_CONFIG_KEYS: dict[str, str] = {
    "ana_maria": "casey_email_ana_maria",
    "tatiana": "casey_email_tatiana",
    "chris": "casey_email_chris",
}

# When set, replaces every email recipient below (local testing only).
EMAIL_OVERRIDE_CONFIG_KEY = "casey_email_override"

SLACK_URGENTMATTERS_RECIPIENT = "urgentmatters"

_PLANNABLE_ACTIONS = {"create", "escalate", "create_separate"}


def _severity_label(severity: str) -> str:
    return (severity or "").upper()


def build_message(flag: dict) -> str:
    """Severity label first, then customer/project, PM, category, a short
    summary, and a link to the flag record (if given)."""
    severity = flag.get("severity", "")
    lines = [
        f"{_severity_label(severity)}: {flag.get('customer_name', '')}".strip(),
        f"Project: {flag.get('address') or flag.get('job_key', '')}",
        f"PM: {flag.get('pm_name') or 'Unassigned'}",
        f"Category: {flag.get('primary_reason', '')}",
    ]
    if flag.get("issue_summary"):
        lines.append(flag["issue_summary"])
    if flag.get("link"):
        lines.append(flag["link"])
    return "\n".join(lines)


def plan_notifications(
    flag: dict,
    action: str,
    *,
    recipient_addresses: "dict[str, str] | None" = None,
    email_matrix: "dict[str, list[str]] | None" = None,
    pm_email: str = "",
) -> list[dict[str, Any]]:
    """Returns the rows to create for this flag/action: a Slack channel
    post and a Slack DM to the PM for every plannable action, plus one
    email row per (PM + matrix recipient for this severity) with a
    resolved, non-empty address.

    flag: needs at least id, severity, job_key, customer_name, pm_name,
        primary_reason; address/issue_summary/link are optional.
    action: one of agents.casey.flag_logic.ACTIONS. Only "create",
        "escalate" and "create_separate" plan anything — "no_op" and
        "note_improvement" plan nothing (an improvement is recorded as a
        casey_flag_events row elsewhere, never a notification).
    recipient_addresses: {"ana_maria": "...", "tatiana": "...", "chris":
        "...", "override": "..."} — resolved app_config values. A
        non-empty "override" replaces every email recipient below
        (including the PM's), for local testing; a named recipient with
        no configured address is skipped (never notified).
    email_matrix: severity -> list of recipient_addresses keys that get
        an email at that severity, in addition to the PM. Defaults to
        DEFAULT_EMAIL_MATRIX.
    pm_email: the assigned PM's resolved email address.

    dedup_key is flag_id + severity, so an unchanged flag never alerts
    twice and an escalation (new severity) always produces a fresh
    notification.
    """
    assert set(ACTIONS) >= {action}, f"unknown action {action!r}"
    if action not in _PLANNABLE_ACTIONS:
        return []

    recipient_addresses = recipient_addresses or {}
    email_matrix = email_matrix if email_matrix is not None else DEFAULT_EMAIL_MATRIX
    severity = flag.get("severity", "")
    flag_id = flag.get("id")
    dedup_key = f"{flag_id}:{severity}"
    message = build_message(flag)
    override = recipient_addresses.get("override", "")

    def _row(channel: str, recipient: str) -> dict[str, Any]:
        return {
            "flag_id": flag_id,
            "channel": channel,
            "recipient": recipient,
            "severity": severity,
            "dedup_key": dedup_key,
            "status": "planned",
            "message": message,
        }

    notifications = [_row("slack_channel", SLACK_URGENTMATTERS_RECIPIENT)]
    if flag.get("pm_name"):
        notifications.append(_row("slack_dm", flag["pm_name"]))

    if pm_email:
        notifications.append(_row("email", override or pm_email))
    for key in email_matrix.get(severity, []):
        addr = recipient_addresses.get(key, "")
        if override or addr:
            notifications.append(_row("email", override or addr))

    return notifications


def apply_backfill_suppression(
    notifications: list[dict[str, Any]],
    event_time: "datetime | None",
    live_since: "datetime | None",
) -> list[dict[str, Any]]:
    """Stage 7 backfill rule: any notification for an event (message
    occurred_at, watch due_at, flag event) before `live_since`, OR
    whenever `live_since` is unset entirely, is marked
    status="suppressed", reason="backfill" instead of "planned" — a
    missing event_time is treated the same as "before go-live" (fail
    safe, never guess something is live). Mutates and returns the same
    list of row dicts plan_notifications() built."""
    is_backfill = live_since is None or event_time is None or event_time < live_since
    if is_backfill:
        for row in notifications:
            row["status"] = "suppressed"
            row["reason"] = BACKFILL_REASON
    return notifications
