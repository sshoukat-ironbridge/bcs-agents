"""Creates and evaluates Casey's 24-hour customer-response watches, READ
ONLY against casey_active_jobs. Runnable as:

    docker compose run --rm casey python -m agents.casey.run_watches [--dry-run]

No notifications are ever sent here — an overdue watch's planned
notifications are stored with status "planned" only (never "sent");
actually sending them is a separate, not-yet-built step that must go
through utils.outbound.outbound_enabled().
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.logger import get_logger

logger = get_logger("casey.run_watches")

DEFAULT_WINDOW_HOURS = 24

# A missed response is reported as a "Lack of communication / no updates"
# issue at Medium severity by default — a watch has no complaint-severity
# signal of its own (no taxonomy classification happens here), so this is
# a fixed, documented default rather than a computed one.
WATCH_NOTIFICATION_SEVERITY = "Medium"
WATCH_NOTIFICATION_REASON = "Lack of communication / no updates"


def _create_missing_watches(dry_run: bool) -> "tuple[int, list[dict]]":
    """Returns (created_count, new_watch_dicts). In a dry run, nothing is
    persisted (created_count stays 0) but the would-be watches are still
    built in memory so _evaluate_open_watches() below has something
    meaningful to classify even before any watch has ever been created."""
    from db.state_store import find_unwatched_inbound_messages, create_response_watch, get_config

    window_hours = int(get_config("casey_response_window_hours") or DEFAULT_WINDOW_HOURS)
    created = 0
    new_watches = []
    for message in find_unwatched_inbound_messages():
        received_at = message["occurred_at"]
        if received_at is None:
            continue
        due_at = received_at + timedelta(hours=window_hours)
        watch_id = None
        if not dry_run:
            watch_id = create_response_watch({
                "thread_id": message.get("thread_id"),
                "inbound_message_id": message["id"],
                "job_key": message.get("job_key"),
                "pm_name": message.get("pm_name"),
                "customer_email": message.get("customer_email"),
                "received_at": received_at,
                "due_at": due_at,
            })
            if watch_id is not None:
                created += 1
        new_watches.append({
            "id": watch_id, "thread_id": message.get("thread_id"), "job_key": message.get("job_key"),
            "pm_name": message.get("pm_name"), "customer_email": message.get("customer_email"),
            "received_at": received_at, "due_at": due_at, "status": "waiting",
        })
    return created, new_watches


def _candidates_for_classify(thread_id, job_key) -> list[dict]:
    """Adapts db.state_store.get_response_candidates()'s rows (already
    filtered to the same thread_id/job_key) into response_watch's
    candidate shape. Dialpad calls are matched on job_key, not phone — a
    sentinel "customer_phone" value shared by the synthetic inbound dict
    and every call candidate makes response_watch's own phone-equality
    check pass, since the real same-job filtering already happened in
    the DB query."""
    from db.state_store import get_response_candidates

    rows = get_response_candidates(thread_id, job_key)
    job_sentinel = f"job:{job_key}" if job_key else None
    candidates = []
    for row in rows:
        candidate = dict(row)
        if candidate.get("source") == "dialpad_call":
            candidate["customer_phone"] = job_sentinel
        candidates.append(candidate)
    return candidates


def _plan_overdue_notification(watch: dict, dry_run: bool) -> None:
    from db.state_store import get_config, find_pm, notification_exists, create_notification
    from agents.casey.notification_plan import plan_notifications, RECIPIENT_CONFIG_KEYS

    dedup_key = f"watch:{watch['id']}:overdue"
    if notification_exists(dedup_key):
        return  # already planned once for this watch; never plan twice

    synthetic_flag = {
        "id": None,
        "severity": WATCH_NOTIFICATION_SEVERITY,
        "job_key": watch.get("job_key"),
        "customer_name": watch.get("customer_email"),
        "pm_name": watch.get("pm_name"),
        "primary_reason": WATCH_NOTIFICATION_REASON,
        "issue_summary": f"No PM response within {get_config('casey_response_window_hours') or DEFAULT_WINDOW_HOURS}h of the customer's email.",
    }
    recipient_addresses = {key: (get_config(cfg_key) or "") for key, cfg_key in RECIPIENT_CONFIG_KEYS.items()}
    recipient_addresses["override"] = get_config("casey_email_override") or ""
    pm = find_pm(watch.get("pm_name") or "") if watch.get("pm_name") else None
    pm_email = (pm or {}).get("email", "")

    rows = plan_notifications(synthetic_flag, "create", recipient_addresses=recipient_addresses, pm_email=pm_email)
    if dry_run:
        return
    for row in rows:
        row["watch_id"] = watch["id"]
        row["flag_id"] = None
        row["dedup_key"] = dedup_key  # one shared dedup_key for every row planned from this watch
        create_notification(row)


def _evaluate_open_watches(now: datetime, dry_run: bool, new_watches: list) -> dict[str, int]:
    from db.state_store import get_config, get_open_response_watches, update_response_watch
    from agents.casey.response_watch import classify_response

    window_hours = int(get_config("casey_response_window_hours") or DEFAULT_WINDOW_HOURS)
    sms_counts = (get_config("casey_sms_counts_as_response") or "false").strip().lower() == "true"

    counts = {"waiting": 0, "responded": 0, "overdue": 0, "excluded": 0}

    # A real run already persisted new_watches via create_response_watch()
    # before this function was called, so get_open_response_watches()
    # already includes them — adding new_watches again would double-count
    # them. Only a dry run (which persists nothing) needs them added here,
    # since they otherwise never got evaluated at all.
    watches_to_evaluate = get_open_response_watches() + (new_watches if dry_run else [])
    for watch in watches_to_evaluate:
        candidates = _candidates_for_classify(watch.get("thread_id"), watch.get("job_key"))
        inbound = {
            "received_at": watch["received_at"],
            "thread_id": watch.get("thread_id"),
            "customer_phone": f"job:{watch.get('job_key')}" if watch.get("job_key") else None,
            "is_automated": False,
        }
        status = classify_response(inbound, candidates, now, window_hours=window_hours,
                                    sms_counts_as_response=sms_counts)
        counts[status] += 1

        if status == watch["status"]:
            continue  # no change, nothing to persist

        if status == "responded":
            response_candidate = next(
                (c for c in candidates if c.get("occurred_at") and c["occurred_at"] > watch["received_at"]), None
            )
            response_message_id = (response_candidate or {}).get("message_id")
            if not dry_run:
                update_response_watch(watch["id"], "responded", responded_at=now,
                                       response_message_id=response_message_id)
        elif status == "overdue":
            if not dry_run:
                update_response_watch(watch["id"], "overdue")
            _plan_overdue_notification(watch, dry_run)
        elif not dry_run:
            update_response_watch(watch["id"], status)

    return counts


def run(dry_run: bool = False) -> dict[str, Any]:
    created, new_watches = _create_missing_watches(dry_run)
    now = datetime.now(timezone.utc)
    counts = _evaluate_open_watches(now, dry_run, new_watches)
    return {"created": created, **counts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = run(dry_run=args.dry_run)
    if not args.dry_run:
        print(f"New watches created: {stats['created']}")
    print(f"Waiting: {stats['waiting']}")
    print(f"Responded: {stats['responded']}")
    print(f"Overdue: {stats['overdue']}")
    print(f"Excluded: {stats['excluded']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("run_watches failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
