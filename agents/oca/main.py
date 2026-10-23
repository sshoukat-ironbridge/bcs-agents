import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from agents.oca.checks import (
    check_missing_pm,
)
from agents.oca.alerts import (
    send_deadline_change_alert,
)
from db.state_store import (
    init_db,
    flag_exists,
    create_flag,
    should_alert_again,
    update_flag_alerted,
    get_flag_alert_age_hours,
    resolve_flags_not_in,
    upsert_active_job,
    set_config,
    create_agent_run,
    append_agent_run_log,
    finish_agent_run,
    set_agent_run_summary,
    reconcile_retired_jobs,
)
from integrations.slack import send_message
from integrations.sheets import get_active_jobs
from utils.logger import get_logger
from config.loader import cfg

logger = get_logger("oca")


_ALL_FLAG_TYPES = [
    "missing_pm",
]


def _sync_all(sheet_jobs: list) -> dict:
    """
    OCA sync: keep Google Sheet and local DB aligned.

    Also detects Deadline to Start changes (comparing the DB's previously
    stored value to the sheet's current value) and alerts on them.

    Returns a summary dict of sync counters for the daily digest.
    """
    from db.state_store import get_all_active_jobs
    db_map: dict[tuple[str, str], dict] = {
        (j["client_name"], j.get("job_type") or ""): j for j in get_all_active_jobs()
    }

    synced = 0
    errors = 0

    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue

        pm_name = job.get("pm_name", "")
        job_type = job.get("job_type", "").strip()
        old_deadline = ""

        existing_db = db_map.get((client_name, job_type))
        if existing_db:
            old_deadline = (existing_db.get("deadline_to_start") or "").strip()

        # ── Deadline to Start change detection ──────────────────────────────
        new_deadline = (job.get("deadline_to_start") or "").strip()
        if old_deadline and new_deadline and old_deadline != new_deadline:
            send_deadline_change_alert(client_name, pm_name, old_deadline, new_deadline)
            logger.info(f"Deadline change: {client_name} {old_deadline} → {new_deadline}")

        # ── Upsert to DB ─────────────────────────────────────────────────────
        try:
            upsert_active_job({
                "client_name":            client_name,
                "pm_name":                pm_name,
                "job_type":               job_type,
                "start_date":             job.get("start_date", ""),
                "deposit_date":           job.get("deposit_date", ""),
                "estimated_start_window": job.get("estimated_start_window", ""),
                "assigned_crew_sub":      job.get("assigned_crew_sub", ""),
                "last_pm_contact":        job.get("last_pm_contact"),
                "most_recent_contact":    job.get("sheet_last_contact", ""),
                "pm_communication_history": job.get("pm_communication_history", ""),
                "customer_email":         job.get("email", ""),
                "customer_phone":         job.get("customer_phone", ""),
                "client_mood":            job.get("client_mood", ""),
                "complaint_note":         job.get("complaint_note", ""),
                "job_description":        job.get("job_description", ""),
                "estimator_name":         job.get("estimator_name", ""),
                "to_collect":             job.get("to_collect", ""),
                "total_project":          job.get("total_project", ""),
                "sheet_tab":              job.get("sheet_tab", ""),
                "deadline_to_start":      job.get("deadline_to_start", ""),
                "primary_job_type":       job.get("primary_job_type", ""),
                "comms_hold":             job.get("comms_hold", False),
                "comms_hold_reason":      job.get("comms_hold_reason"),
            })
            synced += 1
        except Exception as e:
            logger.error(f"OCA sync DB error for {client_name}: {e}")
            errors += 1

    set_config("last_sync_at", datetime.utcnow().isoformat())
    logger.info(f"OCA sync complete — sheet jobs: {len(sheet_jobs)}, DB upserted: {synced}, errors: {errors}")
    held_jobs = [j for j in db_map.values() if j.get("comms_hold")]
    logger.info(f"Jobs on comms hold (skipped by OCA): {len(held_jobs)}")
    return {"synced": synced, "errors": errors}


def _checkpoint(run_id: "int | None", line: str) -> None:
    """Append a key log line to this run's agent_runs record, if one exists."""
    if run_id is not None:
        append_agent_run_log(run_id, line + "\n")


def run() -> None:
    # AGENT_RUN_ID is set by the dashboard when it spawns this as a subprocess
    # (it already created the row and owns the live SSE log stream + final
    # status/finished_at) — reuse that row instead of creating a second one.
    # Cron/CLI invocations have no such env var, so they create their own.
    external_run_id = os.getenv("AGENT_RUN_ID")
    own_run = external_run_id is None
    run_id = int(external_run_id) if external_run_id else create_agent_run("oca")

    try:
        summary = _run(run_id)
        set_agent_run_summary(run_id, summary)
        if own_run:
            finish_agent_run(run_id, "success", summary)
    except Exception as e:
        logger.exception("OCA crashed")
        set_agent_run_summary(run_id, f"Error: {e}")
        if own_run:
            finish_agent_run(run_id, "error", f"Error: {e}")
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            msg = (
                f"🔴 Agent crash — OCA failed at {timestamp}\n"
                f"Error: {e}\n"
                f"The next scheduled run will retry automatically."
            )
            urgentmatters = cfg("slack_urgentmatters_channel") or "urgentmatters"
            send_message(urgentmatters, msg)
        except Exception as notify_err:
            print(f"OCA: failed to send crash notification: {notify_err}", file=sys.stderr)
        raise


def _run(run_id: "int | None" = None) -> str:
    if not cfg("slack_bot_token"):
        logger.error("OCA: slack_bot_token not configured in DB — aborting")
        return "Aborted: slack_bot_token not configured"
    if not cfg("google_sheets_id"):
        logger.error("OCA: google_sheets_id not configured in DB — aborting")
        return "Aborted: google_sheets_id not configured"

    logger.info("OCA starting run")
    init_db()

    # ── Step 1: Fetch latest data ────────────────────────────────────────────
    sheet_jobs = get_active_jobs()
    if not sheet_jobs:
        logger.error("OCA: No sheet jobs loaded — aborting")
        return "Aborted: no sheet jobs loaded"

    logger.info(f"OCA loaded {len(sheet_jobs)} sheet jobs")
    _checkpoint(run_id, f"OCA loaded {len(sheet_jobs)} sheet jobs")

    # ── Step 2: Sync — keep Sheet and local DB aligned ──────────────────────
    sync_summary = _sync_all(sheet_jobs)
    _checkpoint(run_id, f"Sync: {sync_summary.get('synced', 0)} jobs upserted, {sync_summary.get('errors', 0)} errors")

    # ── Step 2b: Reconciliation — retire jobs no longer present in sheet ────
    sheet_keys = {
        (j["client_name"], j.get("sheet_tab", ""), j.get("primary_job_type", ""))
        for j in sheet_jobs if j["client_name"].strip()
    }
    retired = reconcile_retired_jobs(sheet_keys)
    if retired:
        logger.info(f"Reconciliation: {retired} job(s) retired — no longer in sheet")
        _checkpoint(run_id, f"Reconciliation: {retired} job(s) retired — no longer in sheet")

    from db.state_store import get_all_active_jobs
    db_jobs = {j["client_name"]: j for j in get_all_active_jobs()}

    # Enrich sheet_jobs with comms_hold from DB so check functions can honour it
    for _job in sheet_jobs:
        _db = db_jobs.get(_job["client_name"], {})
        _job["comms_hold"] = _db.get("comms_hold", False)
        _job["comms_hold_reason"] = _db.get("comms_hold_reason", "")

    # ── Step 3: Run checks ───────────────────────────────────────────────────
    missing_pm_flags = check_missing_pm(sheet_jobs)

    _checkpoint(run_id, f"Checks: missing_pm={len(missing_pm_flags)}")

    all_flags: list[dict] = list(missing_pm_flags)

    logger.info(f"OCA detected {len(all_flags)} flags total")
    _checkpoint(run_id, f"OCA detected {len(all_flags)} flags total")

    # ── Step 4: Process flags with deduplication + cooldown ─────────────────
    alerted = 0
    suppressed = 0

    # Group flags by job_id so cooldown checks are per-job
    flags_by_job: dict[str, list[dict]] = defaultdict(list)
    for flag in all_flags:
        flags_by_job[flag["job_id"]].append(flag)

    for job_id, job_flags in flags_by_job.items():
        for flag in job_flags:
            flag_type = flag["flag_type"]

            if flag_exists(job_id, flag_type):
                if should_alert_again(job_id, flag_type, cooldown_hours=24):
                    update_flag_alerted(job_id, flag_type)
                    alerted += 1
                else:
                    logger.info(f"OCA suppressed (cooldown) {flag_type} for {job_id}")
                    suppressed += 1
            else:
                create_flag(job_id, flag_type, flag.get("details", ""), flag.get("urgency", ""))
                alerted += 1

    # ── Step 5: Auto-resolve flags ───────────────────────────────────────────
    for flag_type in _ALL_FLAG_TYPES:
        flagged_ids = [f["job_id"] for f in all_flags if f["flag_type"] == flag_type]
        resolve_flags_not_in(flagged_ids, flag_type)

    run_summary = f"{len(all_flags)} flags detected, {alerted} alerted, {suppressed} suppressed"
    logger.info(f"OCA run complete — {run_summary}")
    _checkpoint(run_id, f"OCA run complete — {run_summary}")

    return run_summary


if __name__ == "__main__":
    run()
