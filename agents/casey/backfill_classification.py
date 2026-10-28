"""One-off, idempotent backfill: recomputes is_automated/skip_reason for
already-stored dialpad_call rows from their body_text (via
agents.casey.normalizers.classify_call), and recomputes match_status for
every stored call/email row by re-running the job matcher against the
CURRENT job data (old rows where ambiguous vs. unmatched was thrown away
can now be told apart again — see docs/casey_foundation.md).

READ ONLY against external systems; writes only customer_messages'
classification columns on rows that already exist. Runnable as:

    docker compose run --rm casey python -m agents.casey.backfill_classification [--dry-run]
"""
import argparse
import sys
from typing import Any

from utils.logger import get_logger

logger = get_logger("casey.backfill_classification")


def _before_counts() -> dict[str, Any]:
    from db.state_store import _Session, CustomerMessage

    with _Session() as s:
        calls = s.query(CustomerMessage).filter(CustomerMessage.source == "dialpad_call").all()
        counts = {
            "calls_total": len(calls),
            "calls_is_automated_true": sum(1 for r in calls if r.is_automated),
            "calls_skip_reason_set": sum(1 for r in calls if r.skip_reason),
            "match_status_null": s.query(CustomerMessage).filter(CustomerMessage.match_status.is_(None)).count(),
            "match_status_matched": s.query(CustomerMessage).filter(CustomerMessage.match_status == "matched").count(),
            "match_status_ambiguous": s.query(CustomerMessage).filter(CustomerMessage.match_status == "ambiguous").count(),
            "match_status_unmatched": s.query(CustomerMessage).filter(CustomerMessage.match_status == "unmatched").count(),
        }
    return counts


def run(dry_run: bool = False) -> dict[str, Any]:
    from db.state_store import _Session, _db_available, CustomerMessage, get_all_active_jobs
    from agents.casey.normalizers import classify_call, build_contact_index, match_job

    if not _db_available:
        return {"ok": False, "error": "database not available"}

    before = _before_counts()

    jobs = get_all_active_jobs()
    contact_index = build_contact_index(jobs)
    job_id_by_key = {
        (j["client_name"], j.get("sheet_tab") or "", j.get("primary_job_type") or ""): j.get("id")
        for j in jobs
    }

    stats = {
        "calls_reclassified": 0, "calls_is_automated_changed": 0, "calls_skip_reason_changed": 0,
        "match_status_set": 0, "match_status_changed_to_matched": 0,
        "match_status_changed_to_ambiguous": 0, "match_status_changed_to_unmatched": 0,
    }

    with _Session() as s:
        # 1. Call classification, from stored body_text only.
        calls = s.query(CustomerMessage).filter(CustomerMessage.source == "dialpad_call").all()
        for r in calls:
            result = classify_call(r.direction, r.duration_ms, (r.body_text or "").splitlines())
            changed_automated = result["is_automated"] != bool(r.is_automated)
            changed_skip = result["skip_reason"] != r.skip_reason
            stats["calls_reclassified"] += 1
            stats["calls_is_automated_changed"] += int(changed_automated)
            stats["calls_skip_reason_changed"] += int(changed_skip)
            if not dry_run and (changed_automated or changed_skip):
                r.is_automated = result["is_automated"]
                r.skip_reason = result["skip_reason"]

        # 2. match_status, re-derived against CURRENT job data — this can
        # only improve on a row whose ambiguous-vs-unmatched distinction
        # was never persisted in the first place (old rows), or correct
        # one that's gone stale as jobs changed.
        matchable = s.query(CustomerMessage).filter(CustomerMessage.source.in_(["dialpad_call", "gmail"])).all()
        for r in matchable:
            if r.customer_phone:
                match = match_job({"customer_phone": r.customer_phone}, contact_index)
            elif r.customer_email:
                match = match_job({"customer_email": r.customer_email}, contact_index)
            else:
                match = {"job_key": None, "candidates": []}

            if match["job_key"] is not None:
                new_status = "matched"
                new_job_id = job_id_by_key.get(match["job_key"])
            elif match["candidates"]:
                new_status = "ambiguous"
                new_job_id = None
            else:
                new_status = "unmatched"
                new_job_id = None

            if r.match_status is None:
                stats["match_status_set"] += 1
            elif r.match_status != new_status:
                stats[f"match_status_changed_to_{new_status}"] += 1

            if not dry_run:
                r.match_status = new_status
                # Only fill in a previously-unresolved job_id — never
                # overwrite one a real ingest run already set from the
                # data available AT THAT TIME.
                if r.job_id is None and new_job_id is not None:
                    r.job_id = new_job_id

        if not dry_run:
            s.commit()

    after = _before_counts()
    return {"ok": True, "before": before, "after": after, "changes": stats}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    if not result.get("ok"):
        print(f"ERROR: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    print(f"{'DRY RUN — ' if args.dry_run else ''}Before: {result['before']}")
    print(f"Changes: {result['changes']}")
    print(f"After:  {result['after']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("backfill_classification failed")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
