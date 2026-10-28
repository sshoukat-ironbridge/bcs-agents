"""Sends casey_notifications rows with status "planned" through Slack and
Gmail. Runnable as:

    docker compose run --rm casey python -m agents.casey.notify [--dry-run] [--limit N]

Every send goes through:
  - utils.outbound.outbound_enabled() — false under silent_mode or
    OCA_DRY_RUN, in which case rows are left untouched (never claimed).
  - casey_live_since being set — unset means every row is left untouched
    too (this mirrors the backfill-suppression rule: nothing real goes
    out until go-live is explicitly declared).

Real recipients (the PM's own Slack DM / email, or the configured
ana_maria/tatiana/chris addresses) are only used when env CASEY_ENV is
exactly "production". Otherwise every Slack send (channel or DM) goes to
casey_slack_test_channel and every email goes to casey_email_override —
an empty override means every email row is refused rather than guessed
at. A Slack DM is additionally skipped whenever the PM has no configured
slack_user_id, in either mode.

Each row is claimed atomically (planned -> sending, inside one DB
transaction) so two concurrent runs can never double-send the same row.
A failed send is retried up to casey_notify_max_retries times (returned
to "planned" for the next run); once exhausted it is marked "failed"
with the error. A "sent" row is never resent.

Casey never contacts customers: every row this module ever sends came
from agents/casey/notification_plan.py, which only ever plans the
urgentmatters Slack channel, the assigned PM, and the configured
ana_maria/tatiana/chris/override addresses — never a customer email.
"""
import argparse
import os
import sys
from typing import Any

from utils.logger import get_logger

logger = get_logger("casey.notify")

DEFAULT_LIMIT = 50
DEFAULT_MAX_RETRIES = 3


def _is_production() -> bool:
    return os.getenv("CASEY_ENV", "").strip().lower() == "production"


def resolve_target(row: dict, *, test_channel: str, email_override: str,
                    pm_slack_user_ids: dict) -> "tuple[str, bool, str]":
    """Pure resolution of "who/what this row would actually be sent to",
    honouring the production/test-channel/override rules and the PM
    slack_user_id requirement for a DM. Shared by send_one() and
    scripts/casey_send_test.py's dry preview so the preview can never
    drift from what a real send would do.

    Returns (target, ok, note). ok=False means "do not send", and note
    explains why."""
    channel = row["channel"]

    if channel == "slack_channel":
        if _is_production():
            return row["recipient"], True, ""
        if not test_channel:
            return "", False, "no casey_slack_test_channel configured"
        return test_channel, True, ""

    if channel == "slack_dm":
        pm_name = row["recipient"]
        slack_user_id = pm_slack_user_ids.get(pm_name, "")
        if not slack_user_id:
            return "", False, f"no slack_user_id configured for PM {pm_name!r}"
        if _is_production():
            return pm_name, True, ""  # the real DM; send_one resolves slack_user_id itself
        if not test_channel:
            return "", False, "no casey_slack_test_channel configured"
        return test_channel, True, ""

    if channel == "email":
        if _is_production():
            return row["recipient"], True, ""
        if not email_override:
            return "", False, "casey_email_override is empty; refusing to send email outside production"
        return email_override, True, ""

    return "", False, f"unknown channel {channel!r}"


def send_one(row: dict, *, test_channel: str, email_override: str, pm_slack_user_ids: dict) -> "tuple[bool, str]":
    """Sends one notification row. Returns (success, error)."""
    from integrations.slack import send_message, send_dm
    from integrations.gmail import send_email
    from db.state_store import get_config

    channel = row["channel"]
    message = row.get("message") or ""
    target, ok, note = resolve_target(
        row, test_channel=test_channel, email_override=email_override, pm_slack_user_ids=pm_slack_user_ids
    )
    if not ok:
        return False, note

    if channel == "slack_channel":
        sent = send_message(target, message)
        return sent, ("" if sent else "slack send failed")

    if channel == "slack_dm":
        if _is_production():
            slack_user_id = pm_slack_user_ids.get(row["recipient"], "")
            sent = send_dm(slack_user_id, message, pm_name=row["recipient"])
        else:
            sent = send_message(target, message)
        return sent, ("" if sent else "slack dm failed")

    if channel == "email":
        sender_email = get_config("notification_sender_email") or ""
        subject = message.splitlines()[0] if message else "Casey notification"
        body_html = message.replace("\n", "<br>")
        sent, _thread_id, _rfc_message_id = send_email(sender_email, target, subject, body_html)
        return sent, ("" if sent else "email send failed")

    return False, note  # pragma: no cover — resolve_target already rejected unknown channels


def run(dry_run: bool = False, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    from db.state_store import (
        get_config, get_pm_list, get_planned_notifications, claim_notification_for_sending,
        mark_notification_sent, mark_notification_failed,
    )
    from utils.outbound import outbound_enabled
    from agents.casey.ingest_windows import parse_cursor

    stats = {"would_send": 0, "sent": 0, "retrying": 0, "failed": 0, "not_ready": 0}

    live_since = parse_cursor(get_config("casey_live_since"))
    rows = get_planned_notifications(limit=limit)

    if not outbound_enabled() or live_since is None:
        stats["not_ready"] = len(rows)
        logger.info(
            f"notify: outbound disabled or casey_live_since unset — "
            f"leaving {len(rows)} planned row(s) untouched"
        )
        return stats

    if dry_run:
        stats["would_send"] = len(rows)
        return stats

    test_channel = get_config("casey_slack_test_channel") or ""
    email_override = get_config("casey_email_override") or ""
    max_retries = int(get_config("casey_notify_max_retries") or DEFAULT_MAX_RETRIES)
    pm_slack_user_ids = {pm["full_name"]: pm.get("slack_user_id", "") for pm in get_pm_list()}

    for row in rows:
        claimed = claim_notification_for_sending(row["id"])
        if not claimed:
            continue  # already claimed by another concurrent run

        ok, error = send_one(
            claimed, test_channel=test_channel, email_override=email_override, pm_slack_user_ids=pm_slack_user_ids
        )
        if ok:
            mark_notification_sent(claimed["id"])
            stats["sent"] += 1
        else:
            status = mark_notification_failed(claimed["id"], error, max_retries=max_retries)
            if status == "failed":
                logger.error(f"notification {claimed['id']} failed permanently: {error}")
                stats["failed"] += 1
            else:
                logger.warning(f"notification {claimed['id']} failed (will retry): {error}")
                stats["retrying"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    stats = run(dry_run=args.dry_run, limit=args.limit)
    if args.dry_run:
        print(f"Would send: {stats['would_send']}")
    else:
        print(f"Sent: {stats['sent']}")
        print(f"Retrying: {stats['retrying']}")
        print(f"Failed: {stats['failed']}")
    print(f"Not ready (outbound disabled or casey_live_since unset): {stats['not_ready']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("notify failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
