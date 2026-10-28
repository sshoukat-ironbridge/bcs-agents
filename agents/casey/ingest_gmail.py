"""Gmail inbound/outbound ingestion v2, READ ONLY (Casey reads
casey_active_jobs, never writes it; this module never sends/modifies/
deletes mail either — see integrations/gmail_read.py). Runnable as:

    docker compose run --rm casey python -m agents.casey.ingest_gmail --since 14d [--dry-run]

Channel rule (Stage 10a): an incoming message is stored with its full
body and is eligible for analysis. An outgoing message is stored as
METADATA ONLY (time, thread, recipients, subject — empty body) and is
never analyzed; it exists only so the 24h response watch can see a
reply happened.

Mailboxes come from pm_config emails by default (casey_gmail_mailboxes_
mode="pm_config"); "manual" keeps using the casey_gmail_mailboxes list.
Queries are customer-centric (never a whole-mailbox scan): per mailbox,
take that PM's own monitored jobs' customer addresses, batched by
casey_gmail_address_batch_size, and query
  incoming: from:(a OR b ...) after:<epoch> -in:spam -in:trash
  outgoing: to:(a OR b ...) in:sent after:<epoch>
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
DEFAULT_BATCH_SIZE = 15
INGEST_SOURCE_PREFIX = "gmail"

_STAT_KEYS = ("messages_listed", "stored", "skipped_unmatched", "automated",
              "outbound", "inbound", "ambiguous", "cross_pm", "new_rows")


def _empty_stats() -> dict[str, int]:
    return {k: 0 for k in _STAT_KEYS}


def _job_key(job: dict) -> tuple:
    return (job["client_name"], job.get("sheet_tab") or "", job.get("primary_job_type") or "")


def _chunk(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _resolve_mailboxes() -> list[tuple[str, "dict | None"]]:
    """Returns [(email, pm_dict_or_None), ...]. pm_config mode: every
    pm_config row with an email. manual mode: casey_gmail_mailboxes
    (JSON list), matched back to a pm_config row by email when possible
    so job-scoping still works; falls back to None (meaning "scope to
    every active job", the old whole-mailbox behaviour) when it can't."""
    from db.state_store import get_config, get_pm_list

    mode = (get_config("casey_gmail_mailboxes_mode") or "pm_config").strip().lower()
    pm_list = get_pm_list()

    if mode != "manual":
        return [(pm["email"].strip(), pm) for pm in pm_list if (pm.get("email") or "").strip()]

    raw = (get_config("casey_gmail_mailboxes") or "").strip()
    manual_emails: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                manual_emails = [m.strip() for m in parsed if isinstance(m, str) and m.strip()]
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"casey_gmail_mailboxes is not a valid JSON list: {raw!r}, ignoring")
    if not manual_emails:
        default_mailbox = (get_config("notification_sender_email") or "").strip()
        manual_emails = [default_mailbox] if default_mailbox else []

    by_email = {(pm.get("email") or "").strip().lower(): pm for pm in pm_list}
    return [(email, by_email.get(email.strip().lower())) for email in manual_emails]


def _build_incoming_query(addresses: list[str], window: dict) -> str:
    start_s = int(window["start"].timestamp())
    return f"from:({' OR '.join(addresses)}) after:{start_s} -in:spam -in:trash"


def _build_outgoing_query(addresses: list[str], window: dict) -> str:
    start_s = int(window["start"].timestamp())
    return f"to:({' OR '.join(addresses)}) in:sent after:{start_s}"


def _attribute_job(address: "str | None", pm_contact_index: dict, all_contact_index: dict) -> dict:
    """Mailbox-PM's own jobs first. Several of THAT PM's jobs share the
    address -> ambiguous (never guess). No job of that PM for the
    address -> fall back to every active job; exactly one match ->
    attribute it with cross_pm=True; more than one -> ambiguous; none ->
    unmatched."""
    from agents.casey.normalizers import match_job

    if not address:
        return {"job_key": None, "match_status": "unmatched", "cross_pm": False, "candidates": []}

    match = match_job({"customer_email": address}, pm_contact_index)
    if match["job_key"] is not None:
        return {"job_key": match["job_key"], "match_status": "matched", "cross_pm": False, "candidates": match["candidates"]}
    if match["candidates"]:
        return {"job_key": None, "match_status": "ambiguous", "cross_pm": False, "candidates": match["candidates"]}

    fallback = match_job({"customer_email": address}, all_contact_index)
    if fallback["job_key"] is not None:
        return {"job_key": fallback["job_key"], "match_status": "matched", "cross_pm": True, "candidates": fallback["candidates"]}
    if fallback["candidates"]:
        return {"job_key": None, "match_status": "ambiguous", "cross_pm": False, "candidates": fallback["candidates"]}
    return {"job_key": None, "match_status": "unmatched", "cross_pm": False, "candidates": []}


def _resolve_customer_address(direction: str, row: dict, known_emails: set) -> "str | None":
    if direction == "outbound":
        for addr in (row.get("to_addresses") or []) + (row.get("cc_addresses") or []):
            if addr in known_emails:
                return addr
        return None
    from agents.casey.normalizers import _parse_address_list
    from_addrs = _parse_address_list(row.get("sender") or "")
    return from_addrs[0] if from_addrs else None


def _process_message(stub: dict, mailbox: str, direction: str, pm_contact_index: dict, all_contact_index: dict,
                      job_by_key: dict, job_id_by_key: dict, store_unmatched: bool, stats: dict) -> "dict | None":
    from db.state_store import get_customer_message_raw_ref
    from integrations.gmail_read import get_message, get_message_metadata
    from agents.casey.normalizers import normalize_gmail_message, is_automated_email

    if direction == "inbound":
        message = get_message(mailbox, stub["id"])
        row = normalize_gmail_message(message)
    else:
        message = get_message_metadata(mailbox, stub["id"])
        row = normalize_gmail_message(message)
        row["body_text"] = ""  # metadata only, never a body to analyze

    row["direction"] = direction
    stats["outbound" if direction == "outbound" else "inbound"] += 1

    if direction == "inbound" and row["is_automated"]:
        stats["automated"] += 1
        return None

    all_known = set(all_contact_index["by_email"].keys())
    candidate_address = _resolve_customer_address(direction, row, all_known)
    attribution = _attribute_job(candidate_address, pm_contact_index, all_contact_index)

    if attribution["match_status"] == "unmatched":
        stats["skipped_unmatched"] += 1
        if not store_unmatched:
            return None
    elif attribution["match_status"] == "ambiguous":
        stats["ambiguous"] += 1
    if attribution["cross_pm"]:
        stats["cross_pm"] += 1

    job_key_tuple = attribution["job_key"]
    job = job_by_key.get(job_key_tuple) if job_key_tuple else None

    row["job_key"] = "|".join(job_key_tuple) if job_key_tuple else None
    row["job_id"] = job_id_by_key.get(job_key_tuple) if job_key_tuple else None
    row["pm_name"] = job.get("pm_name") if job else None
    row["estimator_name"] = job.get("estimator_name") if job else None
    row["match_status"] = attribution["match_status"]

    candidate_job_keys = ["|".join(k) for k in attribution["candidates"]]
    mailboxes_seen = {mailbox}
    existing_raw_ref = get_customer_message_raw_ref("gmail", row["external_id"])
    if existing_raw_ref:
        try:
            prior = json.loads(existing_raw_ref)
            mailboxes_seen |= set(prior.get("mailboxes_seen") or [])
        except (json.JSONDecodeError, TypeError):
            pass

    row["raw_ref"] = json.dumps({
        "mailboxes_seen": sorted(mailboxes_seen), "candidate_job_keys": candidate_job_keys,
        "cross_pm": attribution["cross_pm"],
    })

    stats["stored"] += 1
    return row


def run(since, dry_run: bool = False) -> dict[str, Any]:
    from db.state_store import (
        get_config, get_all_active_jobs, get_ingest_cursor, set_ingest_cursor,
        upsert_customer_message, create_ingest_run_stats,
    )
    from agents.casey.normalizers import build_contact_index

    max_messages = int(get_config("casey_gmail_max_messages") or DEFAULT_MAX_MESSAGES)
    overlap_hours = float(get_config("casey_gmail_overlap_hours") or DEFAULT_OVERLAP_HOURS)
    store_unmatched = (get_config("casey_gmail_store_unmatched") or "false").strip().lower() == "true"
    batch_size = int(get_config("casey_gmail_address_batch_size") or DEFAULT_BATCH_SIZE)

    jobs = [j for j in get_all_active_jobs() if not j.get("retired")]
    all_contact_index = build_contact_index(jobs)
    job_id_by_key = {_job_key(j): j.get("id") for j in jobs}
    job_by_key = {_job_key(j): j for j in jobs}

    mailboxes = _resolve_mailboxes()
    if not mailboxes:
        logger.warning("No Gmail mailboxes resolved (pm_config has no emails, and no manual list); nothing to ingest")

    overall = _empty_stats()
    per_mailbox: dict[str, dict] = {}
    per_pm_counts: dict[str, int] = {}

    from db.state_store import get_pm_list, find_pm
    pm_list = get_pm_list()

    for mailbox, pm in mailboxes:
        # casey_active_jobs.pm_name is a raw sheet alias, not pm_config's
        # canonical name — resolve through find_pm() the same way
        # dashboard/casey_queries.py's people_workload() already does.
        pm_jobs = (
            [j for j in jobs if (find_pm(j.get("pm_name") or "", pm_list) or {}).get("full_name") == pm["full_name"]]
            if pm else jobs
        )
        pm_contact_index = build_contact_index(pm_jobs)
        customer_emails = sorted(pm_contact_index["by_email"].keys())

        cursor_source = f"{INGEST_SOURCE_PREFIX}:{mailbox}"
        cursor_value = get_ingest_cursor(cursor_source)
        now = datetime.now(timezone.utc)
        window = next_window(
            cursor_value, now, overlap_minutes=int(overlap_hours * 60),
            default_lookback_hours=since.total_seconds() / 3600,
        )
        stats = _empty_stats()
        started_at = datetime.now(timezone.utc)

        if customer_emails:
            try:
                for batch in _chunk(customer_emails, batch_size):
                    for direction, query in (
                        ("inbound", _build_incoming_query(batch, window)),
                        ("outbound", _build_outgoing_query(batch, window)),
                    ):
                        remaining = max_messages - stats["messages_listed"]
                        if remaining <= 0:
                            break
                        stubs = list_messages_capped(mailbox, query, remaining)
                        stats["messages_listed"] += len(stubs)

                        for stub in stubs:
                            row = _process_message(
                                stub, mailbox, direction, pm_contact_index, all_contact_index,
                                job_by_key, job_id_by_key, store_unmatched, stats,
                            )
                            if row is None or dry_run:
                                continue
                            if upsert_customer_message(row):
                                stats["new_rows"] += 1
            except Exception:
                logger.exception(f"Gmail ingestion failed partway through mailbox {mailbox!r}; cursor will NOT advance")
                raise
        else:
            logger.info(f"Mailbox {mailbox!r}: no monitored job customer addresses, nothing to query")

        if not dry_run:
            set_ingest_cursor(cursor_source, window["end"].isoformat())
            create_ingest_run_stats({
                "source": "gmail", "mailbox_or_pm": mailbox,
                "started_at": started_at, "finished_at": datetime.now(timezone.utc), "counters": stats,
            })

        for key in _STAT_KEYS:
            overall[key] += stats[key]
        per_mailbox[mailbox] = stats
        per_pm_counts[(pm or {}).get("full_name", mailbox)] = stats["stored"]

    overall["per_mailbox"] = per_mailbox
    overall["per_pm_counts"] = per_pm_counts
    return overall


def list_messages_capped(mailbox: str, query: str, max_results: int) -> list[dict]:
    from integrations.gmail_read import list_messages
    return list_messages(mailbox, query, max_results=max_results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="First-run lookback, e.g. '14d', '12h'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = run(parse_since(args.since), dry_run=args.dry_run)

    if args.dry_run:
        print("Dry run — counts per PM/mailbox only:")
        for pm_name, count in stats["per_pm_counts"].items():
            print(f"  {pm_name}: {count} candidate message(s)")
        return

    print(f"Messages listed: {stats['messages_listed']}")
    print(f"Stored: {stats['stored']}")
    print(f"Skipped unmatched: {stats['skipped_unmatched']}")
    print(f"Automated: {stats['automated']}")
    print(f"Outbound: {stats['outbound']}")
    print(f"Inbound: {stats['inbound']}")
    print(f"Ambiguous: {stats['ambiguous']}")
    print(f"Cross-PM matches: {stats['cross_pm']}")
    print(f"New rows: {stats['new_rows']}")
    print(f"Per-PM stored counts: {stats['per_pm_counts']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("ingest_gmail failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
