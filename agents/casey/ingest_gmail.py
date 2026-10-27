"""Gmail inbound/outbound ingestion, READ ONLY (Casey reads
casey_active_jobs, never writes it; this module never sends/modifies/
deletes mail either — see integrations/gmail_read.py). Runnable as:

    docker compose run --rm casey python -m agents.casey.ingest_gmail --since 7d [--dry-run]
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from utils.logger import get_logger
from agents.casey.ingest_windows import parse_since, next_window

logger = get_logger("casey.ingest_gmail")

DEFAULT_MAX_MESSAGES = 500
DEFAULT_OVERLAP_HOURS = 2
INGEST_SOURCE_PREFIX = "gmail"

_STAT_KEYS = ("messages_listed", "stored", "skipped_unmatched", "automated",
              "outbound", "inbound", "ambiguous", "new_rows")


def _empty_stats() -> dict[str, int]:
    return {k: 0 for k in _STAT_KEYS}


def _resolve_mailboxes() -> list[str]:
    """casey_gmail_mailboxes (app_config, JSON list) if set and valid;
    otherwise ONLY the currently configured notification_sender_email —
    never a PM's own mailbox."""
    from db.state_store import get_config

    raw = (get_config("casey_gmail_mailboxes") or "").strip()
    if raw:
        try:
            mailboxes = json.loads(raw)
            if isinstance(mailboxes, list) and mailboxes:
                return [m.strip() for m in mailboxes if isinstance(m, str) and m.strip()]
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"casey_gmail_mailboxes is not a valid JSON list: {raw!r}, ignoring")

    default_mailbox = (get_config("notification_sender_email") or "").strip()
    return [default_mailbox] if default_mailbox else []


def _build_query(window: dict) -> str:
    start_s = int(window["start"].timestamp())
    end_s = int(window["end"].timestamp())
    return f"after:{start_s} before:{end_s} -in:spam -in:trash"


def _resolve_customer_address(row: dict, known_emails: set) -> "str | None":
    """Outbound: the first To/Cc address that's a known customer email.
    Inbound: the sender's bare address, whether known or not (match_job()
    then decides matched/ambiguous/unmatched)."""
    if row["direction"] == "outbound":
        for addr in (row.get("to_addresses") or []) + (row.get("cc_addresses") or []):
            if addr in known_emails:
                return addr
        return None

    from agents.casey.normalizers import _parse_address_list
    from_addrs = _parse_address_list(row.get("sender") or "")
    return from_addrs[0] if from_addrs else None


def _process_message(stub: dict, mailbox: str, known_emails: set, contact_index: dict,
                      job_by_key: dict, job_id_by_key: dict, store_unmatched: bool,
                      stats: dict) -> "dict | None":
    from integrations.gmail_read import get_message
    from agents.casey.normalizers import normalize_gmail_message, match_job

    message = get_message(mailbox, stub["id"])
    row = normalize_gmail_message(message)

    stats["outbound" if row["direction"] == "outbound" else "inbound"] += 1

    if row["is_automated"]:
        stats["automated"] += 1
        return None

    candidate_address = _resolve_customer_address(row, known_emails)
    if candidate_address is None:
        match = {"job_key": None, "candidates": []}
    else:
        match = match_job({"customer_email": candidate_address}, contact_index)

    if not match["candidates"]:
        stats["skipped_unmatched"] += 1
        if not store_unmatched:
            return None

    if len(match["candidates"]) >= 2:
        stats["ambiguous"] += 1

    job_key_tuple = match["job_key"]
    job = job_by_key.get(job_key_tuple) if job_key_tuple else None

    row["job_key"] = "|".join(job_key_tuple) if job_key_tuple else None
    row["job_id"] = job_id_by_key.get(job_key_tuple) if job_key_tuple else None
    row["pm_name"] = job.get("pm_name") if job else None
    row["estimator_name"] = job.get("estimator_name") if job else None

    candidate_job_keys = ["|".join(k) for k in match["candidates"]]
    row["raw_ref"] = json.dumps({"message": message, "candidate_job_keys": candidate_job_keys})

    stats["stored"] += 1
    return row


def run(since, dry_run: bool = False) -> dict[str, Any]:
    from db.state_store import (
        get_config, get_all_active_jobs, get_ingest_cursor, set_ingest_cursor,
        upsert_customer_message,
    )
    from agents.casey.normalizers import build_contact_index
    from integrations.gmail_read import list_messages

    max_messages = int(get_config("casey_gmail_max_messages") or DEFAULT_MAX_MESSAGES)
    overlap_hours = float(get_config("casey_gmail_overlap_hours") or DEFAULT_OVERLAP_HOURS)
    store_unmatched = (get_config("casey_gmail_store_unmatched") or "false").strip().lower() == "true"

    jobs = get_all_active_jobs()
    contact_index = build_contact_index(jobs)
    known_emails = set(contact_index["by_email"].keys())
    job_id_by_key = {
        (j["client_name"], j.get("sheet_tab") or "", j.get("primary_job_type") or ""): j.get("id")
        for j in jobs
    }
    job_by_key = {
        (j["client_name"], j.get("sheet_tab") or "", j.get("primary_job_type") or ""): j
        for j in jobs
    }

    mailboxes = _resolve_mailboxes()
    if not mailboxes:
        logger.warning("No Gmail mailbox configured (casey_gmail_mailboxes / notification_sender_email); nothing to ingest")

    overall = _empty_stats()
    per_mailbox: dict[str, dict] = {}

    for mailbox in mailboxes:
        cursor_source = f"{INGEST_SOURCE_PREFIX}:{mailbox}"
        cursor_value = get_ingest_cursor(cursor_source)
        now = datetime.now(timezone.utc)
        window = next_window(
            cursor_value, now,
            overlap_minutes=int(overlap_hours * 60),
            default_lookback_hours=since.total_seconds() / 3600,
        )
        query = _build_query(window)
        stats = _empty_stats()

        try:
            stubs = list_messages(mailbox, query, max_results=max_messages)
            stats["messages_listed"] = len(stubs)

            for stub in stubs:
                row = _process_message(
                    stub, mailbox, known_emails, contact_index,
                    job_by_key, job_id_by_key, store_unmatched, stats,
                )
                if row is None or dry_run:
                    continue
                if upsert_customer_message(row):
                    stats["new_rows"] += 1
        except Exception:
            logger.exception(f"Gmail ingestion failed partway through mailbox {mailbox!r}; cursor will NOT advance")
            raise

        if not dry_run:
            set_ingest_cursor(cursor_source, window["end"].isoformat())
            logger.info(f"Cursor for {cursor_source} advanced to {window['end'].isoformat()}")

        for key in _STAT_KEYS:
            overall[key] += stats[key]
        per_mailbox[mailbox] = stats

    overall["per_mailbox"] = per_mailbox
    return overall


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="First-run lookback, e.g. '7d', '12h', '30m'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = run(parse_since(args.since), dry_run=args.dry_run)

    print(f"Messages listed: {stats['messages_listed']}")
    print(f"Stored candidates: {stats['stored']}")
    print(f"Skipped unmatched: {stats['skipped_unmatched']}")
    print(f"Automated: {stats['automated']}")
    print(f"Outbound: {stats['outbound']}")
    print(f"Inbound: {stats['inbound']}")
    print(f"Ambiguous: {stats['ambiguous']}")
    if not args.dry_run:
        print(f"New rows: {stats['new_rows']}")
    print(f"Per-mailbox: {stats['per_mailbox']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("ingest_gmail failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
