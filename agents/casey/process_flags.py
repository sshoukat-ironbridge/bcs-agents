"""Applies flag_logic to every unprocessed red-flag analysis: creates/
escalates/notes casey_flags + casey_flag_events (actor "casey"), and
plans (never sends) notifications, honouring backfill suppression.
Improving sentiment is only ever recorded as an event — it never closes
or lowers a flag. Humans only change status or lower severity (not done
here).
"""
import sys
from typing import Any

from utils.logger import get_logger

logger = get_logger("casey.process_flags")

PLANNABLE_ACTIONS = ("create", "escalate", "create_separate")


def _candidate_from_analysis(analysis: dict) -> dict:
    return {
        "job_key": analysis.get("job_key"),
        "primary_reason": analysis.get("primary_reason"),
        "severity": analysis.get("severity"),
        "red_flag_suggested": True,
        "sentiment": analysis.get("sentiment"),
    }


def _dedup_key(job_key: "str | None", primary_reason: "str | None") -> str:
    return f"{job_key}:{primary_reason}"


def _flag_link(flag: dict) -> str:
    from db.state_store import get_config

    base_url = (get_config("casey_dashboard_base_url") or "").rstrip("/")
    if not base_url or flag.get("id") is None:
        return ""
    return f"{base_url}/casey/flags/{flag['id']}"


def _plan_and_store_notifications(flag: dict, action: str, event_time, dry_run: bool,
                                   old_severity: "str | None" = None) -> int:
    from db.state_store import get_config, find_pm, create_notification
    from agents.casey.notification_plan import plan_notifications, apply_backfill_suppression, RECIPIENT_CONFIG_KEYS
    from agents.casey.ingest_windows import parse_cursor

    recipient_addresses = {key: (get_config(cfg_key) or "") for key, cfg_key in RECIPIENT_CONFIG_KEYS.items()}
    recipient_addresses["override"] = get_config("casey_email_override") or ""
    pm = find_pm(flag.get("pm_name") or "") if flag.get("pm_name") else None
    pm_email = (pm or {}).get("email", "")

    flag_with_link = {**flag, "link": _flag_link(flag)}
    rows = plan_notifications(flag_with_link, action, recipient_addresses=recipient_addresses,
                               pm_email=pm_email, old_severity=old_severity)
    live_since = parse_cursor(get_config("casey_live_since"))
    rows = apply_backfill_suppression(rows, event_time, live_since)

    if dry_run:
        return len(rows)
    for row in rows:
        row["flag_id"] = flag["id"]
        row["watch_id"] = None
        create_notification(row)
    return len(rows)


def process_one(analysis: dict, dry_run: bool = False) -> dict[str, Any]:
    """Returns {"action", "flag_id", "notifications_planned"}."""
    from db.state_store import get_open_flags_for_job, get_flag_by_id, create_flag_row, update_flag_row, create_flag_event
    from agents.casey.flag_logic import decide_action

    job_key = analysis.get("job_key")
    candidate = _candidate_from_analysis(analysis)
    existing_open_flags = get_open_flags_for_job(job_key)

    decision = decide_action(existing_open_flags, candidate)
    action = decision["action"]
    matched_flag = decision["matched_flag"]
    message_id = analysis.get("message_id")

    result = {"action": action, "flag_id": None, "notifications_planned": 0}

    if action == "no_op":
        if not dry_run:
            create_flag_event({
                "flag_id": matched_flag["id"] if matched_flag else None,
                "event_type": "note", "old_value": None, "new_value": "no_op",
                "actor": "casey", "message_id": message_id,
            })
        return result

    if action == "note_improvement":
        result["flag_id"] = matched_flag["id"] if matched_flag else None
        if not dry_run and matched_flag:
            create_flag_event({
                "flag_id": matched_flag["id"], "event_type": "improving_sentiment",
                "old_value": None, "new_value": analysis.get("sentiment"),
                "actor": "casey", "message_id": message_id,
            })
        return result

    if action == "escalate":
        result["flag_id"] = matched_flag["id"] if matched_flag else None
        if not dry_run and matched_flag:
            old_severity = matched_flag["severity"]
            update_flag_row(matched_flag["id"], severity=candidate["severity"], latest_message_id=message_id)
            create_flag_event({
                "flag_id": matched_flag["id"], "event_type": "severity_escalated",
                "old_value": old_severity, "new_value": candidate["severity"],
                "actor": "casey", "message_id": message_id,
            })
            flag_row = get_flag_by_id(matched_flag["id"])
            result["notifications_planned"] = _plan_and_store_notifications(
                flag_row, action, analysis.get("occurred_at"), dry_run, old_severity=old_severity
            )
        return result

    if action in ("create", "create_separate"):
        dedup_key = _dedup_key(job_key, candidate["primary_reason"])
        if not dry_run:
            flag_id = create_flag_row({
                "job_id": analysis.get("job_id"), "job_key": job_key,
                "customer_name": analysis.get("customer_name"), "pm_name": analysis.get("pm_name"),
                "estimator_name": analysis.get("estimator_name"), "severity": candidate["severity"],
                "primary_reason": candidate["primary_reason"], "additional_reasons": analysis.get("additional_reasons") or [],
                "root_cause": analysis.get("root_cause") or "Undetermined (default)",
                "reason_notes": analysis.get("reason_notes"), "issue_summary": analysis.get("issue_summary"),
                "dedup_key": dedup_key, "first_message_id": message_id, "latest_message_id": message_id,
            })
            result["flag_id"] = flag_id
            if flag_id is not None:
                create_flag_event({
                    "flag_id": flag_id, "event_type": "created", "old_value": None,
                    "new_value": candidate["severity"], "actor": "casey", "message_id": message_id,
                })
                flag_row = get_flag_by_id(flag_id)
                result["notifications_planned"] = _plan_and_store_notifications(
                    flag_row, action, analysis.get("occurred_at"), dry_run
                )
        return result

    return result  # pragma: no cover — decide_action only ever returns the actions above


def run(dry_run: bool = False) -> dict[str, Any]:
    from db.state_store import get_unprocessed_analyses

    stats = {
        "processed": 0, "create": 0, "create_separate": 0, "escalate": 0,
        "note_improvement": 0, "no_op": 0, "notifications_planned": 0,
    }
    for analysis in get_unprocessed_analyses():
        result = process_one(analysis, dry_run=dry_run)
        stats["processed"] += 1
        stats[result["action"]] += 1
        stats["notifications_planned"] += result["notifications_planned"]

    return stats


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = run(dry_run=args.dry_run)
    print(f"Processed: {stats['processed']}")
    print(f"Created: {stats['create']}")
    print(f"Created separate: {stats['create_separate']}")
    print(f"Escalated: {stats['escalate']}")
    print(f"Noted improvement: {stats['note_improvement']}")
    print(f"No-op: {stats['no_op']}")
    print(f"Notifications planned: {stats['notifications_planned']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("process_flags failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
