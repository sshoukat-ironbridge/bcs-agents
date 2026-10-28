"""Dialpad call ingestion, READ ONLY (Casey reads casey_active_jobs,
never writes it). Runnable as:

    docker compose run --rm casey python -m agents.casey.ingest_dialpad --since 7d [--dry-run]

Gated by app_config casey_dialpad_enabled (default false) for a
*scheduled* run (see is_enabled()/scheduled_run()) — but an explicit CLI
invocation, with or without --dry-run, always proceeds regardless of that
flag.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.logger import get_logger
from agents.casey.ingest_windows import parse_since as _parse_since

logger = get_logger("casey.ingest_dialpad")

DEFAULT_TRANSCRIPT_LAG_MINUTES = 60
DEFAULT_OVERLAP_HOURS = 2
INGEST_SOURCE = "dialpad_call"


def is_enabled() -> bool:
    from db.state_store import get_config
    return (get_config("casey_dialpad_enabled") or "false").strip().lower() == "true"


def _mask_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 4:
        return "***"
    return f"***-***-{digits[-4:]}"


def _compute_window(since: timedelta) -> dict[str, datetime]:
    from db.state_store import get_config, get_ingest_cursor
    from agents.casey.ingest_windows import next_window

    now = datetime.now(timezone.utc)
    transcript_lag = int(get_config("casey_dialpad_transcript_lag_minutes") or DEFAULT_TRANSCRIPT_LAG_MINUTES)
    overlap_hours = float(get_config("casey_dialpad_overlap_hours") or DEFAULT_OVERLAP_HOURS)
    cursor_value = get_ingest_cursor(INGEST_SOURCE)
    adjusted_now = now - timedelta(minutes=transcript_lag)

    return next_window(
        cursor_value, adjusted_now,
        overlap_minutes=int(overlap_hours * 60),
        default_lookback_hours=since.total_seconds() / 3600,
    )


def _pms_with_dialpad_id() -> dict[str, list]:
    """Returns {"usable": [...], "skipped": [...]} — logs a WARNING naming
    each skipped PM."""
    from db.state_store import get_pm_list

    usable, skipped = [], []
    for pm in get_pm_list():
        dialpad_user_id = (pm.get("dialpad_user_id") or "").strip()
        if dialpad_user_id:
            usable.append(pm)
        else:
            skipped.append(pm)
            logger.warning(f"Skipping PM {pm.get('full_name')!r}: no dialpad_user_id configured")
    return {"usable": usable, "skipped": skipped}


def _is_internal_call(call: dict) -> bool:
    """A call is internal (both sides are BCS/Dialpad users) when the
    contact side is itself typed "user" — ASSUMPTION, not separately
    re-confirmed beyond the documented CallContactProto.type field (see
    docs/casey_foundation.md)."""
    contact = call.get("contact") or {}
    return contact.get("type") == "user"


def _is_connected(call: dict) -> bool:
    """duration > 0 is the real "actually connected" signal — LIVE-CHECKED
    against real Dialpad data: `state` is always "hangup" for a concluded
    call (never the "connected" shown as Dialpad's one documented
    example), and date_connected being set correlates exactly with
    duration > 0 across every call sampled. See
    docs/casey_foundation.md's Stage 5 live-check notes."""
    return (call.get("duration") or 0) > 0


def _process_call(call: dict, pm: dict, contact_index: dict, job_id_by_key: dict, job_by_key: dict, stats: dict,
                   store_unmatched: bool = True) -> "dict | None":
    from integrations.dialpad_calls import get_transcript
    from agents.casey.normalizers import normalize_dialpad_transcript, match_job, classify_call

    if _is_internal_call(call):
        stats["skipped_internal"] += 1
        return None

    if not _is_connected(call):
        stats["skipped_not_connected"] += 1
        return None

    transcript = get_transcript(call.get("call_id"))
    if transcript is None or not (transcript.get("lines") or []):
        stats["empty"] += 1
        logger.info(f"No transcript for call_id={call.get('call_id')}")
        return None

    row = normalize_dialpad_transcript(call, transcript)
    if row is None:  # defensive: normalize_dialpad_transcript also treats 0 lines as "nothing to ingest"
        stats["empty"] += 1
        return None

    row["call_state"] = call.get("state")
    row["pm_name"] = pm.get("full_name")
    stats["with_transcript"] += 1

    classification = classify_call(row["direction"], row["duration_ms"], (row["body_text"] or "").splitlines())
    row["is_automated"] = classification["is_automated"]
    row["skip_reason"] = classification["skip_reason"]
    if classification["is_automated"]:
        stats["automated"] += 1

    # Disambiguate a shared phone by the call's own PM — the same rule as
    # email: match_job() alone can't tell two jobs with the same contact
    # apart, but this call is already scoped to one PM's Dialpad line.
    phone = row.get("customer_phone") or ""
    match = match_job({"customer_phone": phone}, contact_index) if phone else {"job_key": None, "candidates": []}

    job_key = match["job_key"]
    if job_key is None and len(match["candidates"]) > 1:
        # Ambiguous by phone alone, but this call is already scoped to
        # one PM's Dialpad line — the same disambiguation rule email
        # uses: if exactly one candidate belongs to this call's PM, that
        # one wins instead of staying ambiguous.
        same_pm_candidates = [
            k for k in match["candidates"]
            if (job_by_key.get(k) or {}).get("pm_name") == pm.get("full_name")
        ]
        if len(same_pm_candidates) == 1:
            job_key = same_pm_candidates[0]

    if job_key is not None:
        row["job_key"] = "|".join(job_key)
        row["job_id"] = job_id_by_key.get(job_key)
        row["match_status"] = "matched"
        stats["matched"] += 1
    elif match["candidates"]:
        row["job_key"] = None
        row["job_id"] = None
        row["match_status"] = "ambiguous"
        stats["ambiguous"] += 1
    else:
        row["job_key"] = None
        row["job_id"] = None
        row["match_status"] = "unmatched"
        stats["unmatched"] += 1

    if row["job_id"] is None and not store_unmatched:
        stats["skipped_unmatched"] = stats.get("skipped_unmatched", 0) + 1
        return None

    return row


def run(since: timedelta, dry_run: bool = False) -> dict[str, Any]:
    from db.state_store import get_config, get_all_active_jobs, upsert_customer_message, set_ingest_cursor, create_ingest_run_stats
    from agents.casey.normalizers import build_contact_index
    from integrations.dialpad_calls import list_calls

    store_unmatched = (get_config("casey_dialpad_store_unmatched") or "false").strip().lower() == "true"

    stats = {
        "calls_seen": 0, "with_transcript": 0, "empty": 0,
        "skipped_internal": 0, "skipped_not_connected": 0,
        "matched": 0, "ambiguous": 0, "unmatched": 0, "skipped_unmatched": 0,
        "automated": 0, "new_rows": 0,
    }
    samples: list[dict] = []

    window = _compute_window(since)
    pm_info = _pms_with_dialpad_id()
    pms = pm_info["usable"]
    if not pms:
        logger.warning("No PMs have a dialpad_user_id configured; nothing to ingest")

    jobs = get_all_active_jobs()
    contact_index = build_contact_index(jobs)
    job_id_by_key = {
        (j["client_name"], j.get("sheet_tab") or "", j.get("primary_job_type") or ""): j.get("id")
        for j in jobs
    }
    job_by_key = {
        (j["client_name"], j.get("sheet_tab") or "", j.get("primary_job_type") or ""): j
        for j in jobs
    }

    start_ms = int(window["start"].timestamp() * 1000)
    end_ms = int(window["end"].timestamp() * 1000)
    per_pm_counts: dict[str, int] = {}

    try:
        for pm in pms:
            pm_name = pm.get("full_name", "")
            started_at = datetime.now(timezone.utc)
            pm_stats = {
                "calls_seen": 0, "with_transcript": 0, "empty": 0, "skipped_internal": 0,
                "skipped_not_connected": 0, "matched": 0, "ambiguous": 0, "unmatched": 0,
                "skipped_unmatched": 0, "automated": 0, "new_rows": 0,
            }
            calls = list_calls(pm["dialpad_user_id"], start_ms, end_ms)
            per_pm_counts[pm_name] = len(calls)
            pm_stats["calls_seen"] = len(calls)

            for call in calls:
                row = _process_call(call, pm, contact_index, job_id_by_key, job_by_key, pm_stats, store_unmatched)
                if row is None:
                    continue

                if dry_run:
                    if len(samples) < 3:
                        samples.append({
                            "customer": (row.get("body_text") or "")[:80],
                            "phone": _mask_phone(row.get("customer_phone") or ""),
                            "pm_name": row.get("pm_name"),
                            "matched_job_key": row.get("job_key"),
                        })
                    continue

                inserted = upsert_customer_message(row)
                if inserted:
                    pm_stats["new_rows"] += 1

            for key in stats:
                stats[key] += pm_stats.get(key, 0)

            if not dry_run:
                create_ingest_run_stats({
                    "source": INGEST_SOURCE, "mailbox_or_pm": pm_name,
                    "started_at": started_at, "finished_at": datetime.now(timezone.utc),
                    "counters": pm_stats,
                })
    except Exception:
        logger.exception("Dialpad ingestion failed partway through the window; cursor will NOT advance")
        raise

    if not dry_run:
        set_ingest_cursor(INGEST_SOURCE, window["end"].isoformat())
        logger.info(f"Cursor advanced to {window['end'].isoformat()}")

    stats["window_start"] = window["start"].isoformat()
    stats["window_end"] = window["end"].isoformat()
    stats["per_pm_counts"] = per_pm_counts
    stats["pms_skipped_missing_dialpad_id"] = len(pm_info["skipped"])
    stats["samples"] = samples
    return stats


def purge_unmatched_calls(dry_run: bool = True) -> dict[str, int]:
    """One-off local cleanup: deletes stored dialpad_call rows with no
    job_id, plus any message_analyses/casey_flag_events/casey_flags
    attached to them (a flag can only exist from a matched, analyzed
    message, but this is defensive in case that ever changes). Never
    touches gmail/dialpad_sms rows or anything already is_demo."""
    from db.state_store import (
        _Session, _db_available, CustomerMessage, MessageAnalysis, CaseyFlagEvent, CaseyFlag,
    )

    if not _db_available:
        return {"messages": 0, "analyses": 0, "flag_events": 0, "flags": 0}

    with _Session() as s:
        unmatched_ids = [
            r.id for r in s.query(CustomerMessage.id).filter(
                CustomerMessage.source == "dialpad_call", CustomerMessage.job_id.is_(None),
                CustomerMessage.is_demo == False,  # noqa: E712
            ).all()
        ]
        if not unmatched_ids:
            return {"messages": 0, "analyses": 0, "flag_events": 0, "flags": 0}

        flag_ids = [
            r.id for r in s.query(CaseyFlag.id).filter(
                (CaseyFlag.first_message_id.in_(unmatched_ids)) | (CaseyFlag.latest_message_id.in_(unmatched_ids))
            ).all()
        ]
        counts = {
            "messages": len(unmatched_ids),
            "analyses": s.query(MessageAnalysis).filter(MessageAnalysis.message_id.in_(unmatched_ids)).count(),
            "flag_events": s.query(CaseyFlagEvent).filter(CaseyFlagEvent.message_id.in_(unmatched_ids)).count(),
            "flags": len(flag_ids),
        }
        if not dry_run:
            if flag_ids:
                s.query(CaseyFlagEvent).filter(CaseyFlagEvent.flag_id.in_(flag_ids)).delete(synchronize_session=False)
                s.query(CaseyFlag).filter(CaseyFlag.id.in_(flag_ids)).delete(synchronize_session=False)
            s.query(CaseyFlagEvent).filter(CaseyFlagEvent.message_id.in_(unmatched_ids)).delete(synchronize_session=False)
            s.query(MessageAnalysis).filter(MessageAnalysis.message_id.in_(unmatched_ids)).delete(synchronize_session=False)
            s.query(CustomerMessage).filter(CustomerMessage.id.in_(unmatched_ids)).delete(synchronize_session=False)
            s.commit()
        return counts


def scheduled_run(since: timedelta) -> "dict | None":
    """For a future scheduler: gated by casey_dialpad_enabled. An explicit
    CLI invocation does NOT go through this function — main() calls
    run() directly regardless of the flag."""
    if not is_enabled():
        logger.info("casey_dialpad_enabled is false; skipping scheduled Dialpad ingestion")
        return None
    return run(since)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="First-run lookback, e.g. '7d', '12h', '30m'")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--purge-unmatched", action="store_true",
                         help="One-off: delete stored dialpad_call rows with no job_id (and their analyses/flags). Implies --dry-run unless combined with no other flag changes.")
    args = parser.parse_args()

    if args.purge_unmatched:
        counts = purge_unmatched_calls(dry_run=args.dry_run)
        print(f"Purge unmatched calls ({'dry run' if args.dry_run else 'APPLIED'}): {counts}")
        return

    if not args.since:
        print("ERROR: --since is required unless --purge-unmatched is given", file=sys.stderr)
        sys.exit(1)

    since = _parse_since(args.since)
    stats = run(since, dry_run=args.dry_run)

    print(f"Window: {stats['window_start']} to {stats['window_end']}")
    print(f"Calls seen: {stats['calls_seen']}")
    print(f"With transcript: {stats['with_transcript']}")
    print(f"Empty (no transcript): {stats['empty']}")
    print(f"Skipped internal: {stats['skipped_internal']}")
    print(f"Skipped not connected: {stats['skipped_not_connected']}")
    print(f"Matched: {stats['matched']}")
    print(f"Ambiguous: {stats['ambiguous']}")
    print(f"Unmatched: {stats['unmatched']}")
    print(f"Skipped (unmatched, not stored): {stats['skipped_unmatched']}")
    print(f"Automated (voicemail/greeting): {stats['automated']}")
    if not args.dry_run:
        print(f"New rows: {stats['new_rows']}")
    print(f"Per-PM call counts: {stats['per_pm_counts']}")
    print(f"PMs skipped (missing dialpad_user_id): {stats['pms_skipped_missing_dialpad_id']}")

    if args.dry_run and stats["samples"]:
        print("\nSamples (dry run, text truncated to 80 chars, phone masked):")
        for sample in stats["samples"]:
            print(f"  - PM={sample['pm_name']!r} phone={sample['phone']!r} "
                  f"job_key={sample['matched_job_key']!r} text={sample['customer']!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("ingest_dialpad failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
