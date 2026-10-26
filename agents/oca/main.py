import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from agents.oca.checks import (
    check_missing_pm,
    check_deadline_to_start,
    classify_pm_issues,
    DEFAULT_DEADLINE_WARNING_DAYS,
    MAX_RETIRE_COUNT,
    MAX_RETIRE_FRACTION,
    should_skip_retire,
)
from agents.oca.checklist import build_checklist, send_daily_checklist
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
    get_config,
    create_agent_run,
    append_agent_run_log,
    finish_agent_run,
    set_agent_run_summary,
    reconcile_retired_jobs,
    count_retirement_candidates,
)
from integrations.slack import send_message
from integrations.sheets import get_active_jobs, parse_latest_date, SheetFetchError
from utils.logger import get_logger
from utils.timeutil import today_ny
from config.loader import cfg

logger = get_logger("oca")


_ALL_FLAG_TYPES = [
    "missing_pm",
]


def _sheet_sync_key(job: dict) -> tuple:
    """The same (client_name, sheet_tab, primary_job_type) triple
    upsert_active_job() matches DB rows by."""
    return (job["client_name"], job.get("sheet_tab") or "", job.get("primary_job_type") or "")


def collapse_duplicate_sheet_rows(sheet_jobs: list) -> "tuple[list, list]":
    """Collapse sheet_jobs to one row per (client_name, sheet_tab,
    primary_job_type) key — last row wins, matching upsert_active_job()'s
    own behaviour (it's keyed the same way, so the last-processed row for
    a given key always won in the DB anyway). Doing this explicitly,
    BEFORE deadline-change comparison, means a duplicate pair can never
    produce a spurious "changed" alert: only ever one row per key is
    compared against the previous run's stored value.

    Returns (collapsed_jobs, duplicate_groups) where duplicate_groups is
    a list of {"key": (client_name, sheet_tab, primary_job_type), "rows":
    [job, ...]} for every key that had more than one row this run, so the
    caller can log a warning naming what differed between them.
    """
    by_key: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for job in sheet_jobs:
        key = _sheet_sync_key(job)
        if key not in by_key:
            order.append(key)
            by_key[key] = []
        by_key[key].append(job)

    collapsed = [by_key[key][-1] for key in order]
    duplicate_groups = [{"key": key, "rows": rows} for key, rows in by_key.items() if len(rows) > 1]
    return collapsed, duplicate_groups


def _sync_all(sheet_jobs: list) -> dict:
    """
    OCA sync: keep Google Sheet and local DB aligned.

    Sheet rows are first collapsed to one per (client_name, sheet_tab,
    primary_job_type) key (see collapse_duplicate_sheet_rows) — matching
    upsert_active_job()'s own key, and ensuring a duplicate pair can never
    by itself look like a deadline change.

    Also detects Deadline to Start changes (comparing the DB's previously
    stored value — read BEFORE this run's upsert — to the sheet's current
    value, once per key) and alerts on them. Both sides are parsed with
    the shared tolerant date parser (parse_latest_date) before comparing,
    so a date written in a different but equivalent format doesn't look
    like a change; the raw sheet strings are still what gets alerted/stored.

    Returns a summary dict of sync counters, plus deadline_changes: a list
    of {client_name, job_type, pm_name, old_deadline, new_deadline} for the
    run summary.
    """
    sheet_jobs, duplicate_groups = collapse_duplicate_sheet_rows(sheet_jobs)
    for group in duplicate_groups:
        client_name, sheet_tab, primary_job_type = group["key"]
        deadlines = sorted({(r.get("deadline_to_start") or "") for r in group["rows"]})
        pms = sorted({(r.get("pm_name") or "") for r in group["rows"]})
        logger.warning(
            f"Duplicate sheet rows for (client_name={client_name!r}, sheet_tab={sheet_tab!r}, "
            f"primary_job_type={primary_job_type!r}): {len(group['rows'])} rows — "
            f"deadlines seen: {deadlines}, PMs seen: {pms} — using the last row"
        )

    from db.state_store import get_all_active_jobs
    db_map: dict[tuple, dict] = {_sheet_sync_key(j): j for j in get_all_active_jobs()}

    synced = 0
    errors = 0
    deadline_changes: list[dict] = []

    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue

        pm_name = job.get("pm_name", "")
        job_type = job.get("job_type", "").strip()
        old_deadline_raw = ""

        existing_db = db_map.get(_sheet_sync_key(job))
        if existing_db:
            old_deadline_raw = (existing_db.get("deadline_to_start") or "").strip()

        # ── Deadline to Start change detection ──────────────────────────────
        new_deadline_raw = (job.get("deadline_to_start") or "").strip()
        old_deadline_parsed = parse_latest_date(old_deadline_raw) if old_deadline_raw else None
        new_deadline_parsed = parse_latest_date(new_deadline_raw) if new_deadline_raw else None
        if old_deadline_parsed and new_deadline_parsed and old_deadline_parsed != new_deadline_parsed:
            send_deadline_change_alert(client_name, pm_name, old_deadline_raw, new_deadline_raw)
            logger.info(f"Deadline change: {client_name} {old_deadline_raw} → {new_deadline_raw}")
            deadline_changes.append({
                "client_name": client_name,
                "job_type": job_type,
                "pm_name": pm_name,
                "old_deadline": old_deadline_raw,
                "new_deadline": new_deadline_raw,
            })

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
    return {"synced": synced, "errors": errors, "deadline_changes": deadline_changes}


def _checkpoint(run_id: "int | None", line: str) -> None:
    """Append a key log line to this run's agent_runs record, if one exists."""
    if run_id is not None:
        append_agent_run_log(run_id, line + "\n")


def _get_deadline_warning_days() -> int:
    """oca_deadline_warning_days from app_config, default/fallback 7."""
    raw = get_config("oca_deadline_warning_days")
    try:
        return int(raw) if raw and raw.strip() else DEFAULT_DEADLINE_WARNING_DAYS
    except (TypeError, ValueError):
        logger.warning(f"oca_deadline_warning_days={raw!r} is invalid — falling back to {DEFAULT_DEADLINE_WARNING_DAYS}")
        return DEFAULT_DEADLINE_WARNING_DAYS


def _job_key(job: dict) -> tuple:
    return (job["client_name"], job.get("sheet_tab") or "", job.get("primary_job_type") or "")


def enrich_comms_hold(sheet_jobs: list, db_rows: list) -> None:
    """Mutates each sheet_job's comms_hold/comms_hold_reason to match the DB
    row for its exact (client_name, sheet_tab, primary_job_type) key.

    A client can have more than one active job (one per job type/tab), so
    keying by client_name alone — as a previous version of this function
    did — collapses them: whichever DB row survives the dict comprehension
    for that client_name has its comms_hold applied to ALL of that
    client's sheet rows, wrongly clearing a real hold on one job (or
    wrongly applying a hold from one job onto an unrelated one for the
    same client). Pure function over two lists of dicts — no DB access —
    so it's directly unit-testable.
    """
    db_by_key = {_job_key(r): r for r in db_rows}
    for job in sheet_jobs:
        db_row = db_by_key.get(_job_key(job), {})
        job["comms_hold"] = db_row.get("comms_hold", False)
        job["comms_hold_reason"] = db_row.get("comms_hold_reason", "")


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
    # ── Guards ────────────────────────────────────────────────────────────
    if not cfg("slack_bot_token"):
        logger.error("OCA: slack_bot_token not configured in DB — aborting")
        return "Aborted: slack_bot_token not configured"
    if not cfg("google_sheets_id"):
        logger.error("OCA: google_sheets_id not configured in DB — aborting")
        return "Aborted: google_sheets_id not configured"

    logger.info("OCA starting run")
    init_db()

    # ── Read sheet ────────────────────────────────────────────────────────
    # A failed fetch (SheetFetchError) must never be treated like an empty
    # tab — re-raise immediately so run()'s existing crash-alert path fires
    # and the agent_runs record is marked failed, before any upsert/retire.
    try:
        sheet_jobs = get_active_jobs()
    except SheetFetchError as e:
        logger.error(f"OCA: sheet fetch failed — aborting before any upsert/retire: {e}")
        raise
    if not sheet_jobs:
        logger.error("OCA: No sheet jobs loaded — aborting")
        return "Aborted: no sheet jobs loaded"
    logger.info(f"OCA loaded {len(sheet_jobs)} sheet jobs")
    _checkpoint(run_id, f"OCA loaded {len(sheet_jobs)} sheet jobs")

    # ── Sync/upsert (PM resolution + HOLD already applied by sheets.py;
    #    deadline-change collection happens inside _sync_all) ───────────────
    sync_summary = _sync_all(sheet_jobs)
    deadline_changes = sync_summary.get("deadline_changes", [])
    _checkpoint(run_id, f"Sync: {sync_summary.get('synced', 0)} jobs upserted, {sync_summary.get('errors', 0)} errors")

    # ── Retire jobs no longer present in sheet (guarded) ─────────────────
    sheet_keys = {
        (j["client_name"], j.get("sheet_tab", ""), j.get("primary_job_type", ""))
        for j in sheet_jobs if j["client_name"].strip()
    }
    candidate_count, total_active_count = count_retirement_candidates(sheet_keys)
    retire_guard_triggered = should_skip_retire(candidate_count, total_active_count)
    if retire_guard_triggered:
        retired = 0
        logger.warning(
            f"Retire guard: skipping retire — {candidate_count} job(s) would be retired "
            f"out of {total_active_count} active (thresholds: >{MAX_RETIRE_COUNT} count "
            f"or >{MAX_RETIRE_FRACTION:.0%} of active)"
        )
        _checkpoint(run_id, f"Retire guard triggered — skipped retiring {candidate_count} job(s)")
    else:
        retired = reconcile_retired_jobs(sheet_keys)
        if retired:
            logger.info(f"Reconciliation: {retired} job(s) retired — no longer in sheet")
            _checkpoint(run_id, f"Reconciliation: {retired} job(s) retired — no longer in sheet")

    from db.state_store import get_all_active_jobs
    enrich_comms_hold(sheet_jobs, get_all_active_jobs())

    # ── Checks ────────────────────────────────────────────────────────────
    # check_missing_pm's existing flag code path (dedup/cooldown via
    # oca_flags) is untouched — same as before this stage.
    missing_pm_flags = check_missing_pm(sheet_jobs)
    _checkpoint(run_id, f"Checks: missing_pm={len(missing_pm_flags)}")

    flags_by_job: dict[str, list[dict]] = defaultdict(list)
    for flag in missing_pm_flags:
        flags_by_job[flag["job_id"]].append(flag)

    alerted = 0
    suppressed = 0
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

    for flag_type in _ALL_FLAG_TYPES:
        flagged_ids = [f["job_id"] for f in missing_pm_flags if f["flag_type"] == flag_type]
        resolve_flags_not_in(flagged_ids, flag_type)

    # New daily-checklist-only checks (pure functions, fresh snapshot of
    # the current sheet rows — not sourced from oca_flags).
    warning_days = _get_deadline_warning_days()
    deadline_result = check_deadline_to_start(sheet_jobs, today_ny(), warning_days=warning_days)
    pm_issues = classify_pm_issues(sheet_jobs)
    _checkpoint(
        run_id,
        f"Checklist checks: deadline_changes={len(deadline_changes)}, overdue={len(deadline_result['overdue'])}, "
        f"soon={len(deadline_result['soon'])}, blank_pm={len(pm_issues['blank'])}, "
        f"unrecognized_pm={len(pm_issues['unrecognized'])}",
    )

    # ── Build checklist ───────────────────────────────────────────────────
    counts = {
        "to_start": sum(1 for j in sheet_jobs if j.get("sheet_tab") == "to_start"),
        "in_process": sum(1 for j in sheet_jobs if j.get("sheet_tab") == "in_process"),
        "on_hold": sum(1 for j in sheet_jobs if j.get("comms_hold")),
        "retired_this_run": retired,
    }
    checklist = build_checklist(deadline_changes, deadline_result, pm_issues, counts, warning_days)

    # ── Deliver ───────────────────────────────────────────────────────────
    send_daily_checklist(checklist)

    # ── Log summary ───────────────────────────────────────────────────────
    retire_summary = (
        f"retire guard SKIPPED {candidate_count}/{total_active_count}"
        if retire_guard_triggered else f"retired {retired}"
    )
    run_summary = (
        f"missing_pm: {alerted} alerted/{suppressed} suppressed; "
        f"{retire_summary}; "
        f"checklist: {len(deadline_changes)} deadline changes, "
        f"{len(deadline_result['overdue'])} overdue, {len(deadline_result['soon'])} soon, "
        f"{len(pm_issues['blank'])} no PM, {len(pm_issues['unrecognized'])} unrecognized PM"
    )
    logger.info(f"OCA run complete — {run_summary}")
    _checkpoint(run_id, f"OCA run complete — {run_summary}")

    return run_summary


if __name__ == "__main__":
    run()
