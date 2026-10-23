import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

from agents.oca.checks import (
    check_stale_records,
    check_missing_pm,
    check_unconfirmed_crew,
    check_dropped_invoices,
    check_approaching_deadline,
    check_daily_cadence,
)
from agents.oca.alerts import (
    send_deadline_change_alert,
    send_no_email_history_alert,
    send_daily_cadence_report,
)
from db.state_store import (
    init_db,
    flag_exists,
    create_flag,
    should_alert_again,
    update_flag_alerted,
    get_flag_alert_age_hours,
    resolve_flag,
    resolve_flags_not_in,
    upsert_active_job,
    set_config,
    get_email_history,
    upsert_email_history,
    should_fetch_email_history,
    create_agent_run,
    append_agent_run_log,
    finish_agent_run,
    set_agent_run_summary,
    reconcile_retired_jobs,
)
from integrations.slack import send_message
from integrations.sheets import get_active_jobs, parse_latest_date
from integrations.gmail import get_pm_customer_email_history
from utils.logger import get_logger
from config.loader import cfg

logger = get_logger("oca")


_ALL_FLAG_TYPES = [
    "stale_record",
    "missing_pm",
    "unconfirmed_crew",
    "dropped_invoice",
    "approaching_deadline",
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


def _fetch_email_histories(sheet_jobs: list, pm_config: list) -> None:
    """
    Fetch and cache each job's PM→customer Gmail sent-history.

    Used by _build_contact_date_map() for staleness checks and by Casey for
    LLM context. Gmail failures (or zero emails ever found) never block the
    rest of the OCA run — at worst they fall back to the sheet's "Most Recent
    communication" column and/or a once-daily DM to Josh.
    """
    from db.state_store import find_pm
    now = datetime.now(timezone.utc)

    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue
        pm_name = job.get("pm_name", "")
        job_id = f"{client_name}|{pm_name}"
        customer_email = job.get("email", "").strip()

        if not should_fetch_email_history(client_name, pm_name):
            logger.info(f"Email history for {client_name} fetched within 23h — using cached value")
            continue

        pm_row = find_pm(pm_name, pm_config)
        pm_email = pm_row.get("email", "") if pm_row else ""
        if not pm_email:
            logger.warning(f"Email history skip for {client_name} — PM '{pm_name}' has no email in pm_config")
            continue

        if not customer_email:
            logger.warning(f"Email history skip for {client_name} — no customer email on file")
            continue

        prior = get_email_history(client_name, pm_name)
        since = prior.fetched_at if prior else None

        result = get_pm_customer_email_history(pm_email, customer_email, since=since)

        if result:
            upsert_email_history(
                client_name, pm_name, customer_email,
                result["last_sent_at"], result["last_sent_subject"],
                result["email_snippets"], now,
            )
            resolve_flag(job_id, "no_email_history")
            logger.info(
                f"Email history fetched for {client_name}: "
                f"last sent {result['last_sent_at']}, {result['total_found']} emails found"
            )
        elif prior:
            logger.info(f"Gmail returned no new emails for {client_name}, using cached history")
            upsert_email_history(
                client_name, pm_name, prior.customer_email or customer_email,
                prior.last_sent_at, prior.last_sent_subject, prior.email_snippets, now,
            )
        else:
            logger.warning(f"No email history found for {client_name} — Gmail returned zero emails")
            flag_type = "no_email_history"
            if flag_exists(job_id, flag_type):
                if should_alert_again(job_id, flag_type, cooldown_hours=24):
                    update_flag_alerted(job_id, flag_type)
                    send_no_email_history_alert(client_name, pm_name, pm_email, customer_email)
            else:
                create_flag(job_id, flag_type, f"No emails found in {pm_email} sent folder to {customer_email}", "warning")
                send_no_email_history_alert(client_name, pm_name, pm_email, customer_email)


def _fetch_dialpad_histories(sheet_jobs: list) -> None:
    """Fetch and cache Dialpad call/text history for each job that has a customer phone."""
    from integrations.dialpad import get_dialpad_contact_date
    fetched = 0
    for job in sheet_jobs:
        client_name = job["client_name"]
        if not client_name.strip():
            continue
        customer_phone = (job.get("customer_phone") or "").strip()
        if not customer_phone:
            continue
        pm_name = job.get("pm_name", "")
        get_dialpad_contact_date(client_name, pm_name, customer_phone)
        fetched += 1
    logger.info(f"Dialpad histories fetched/refreshed for {fetched} jobs")


def _build_contact_date_map(sheet_jobs: list) -> dict:
    """Returns {client_name: last_contact_date | None}.

    Sources in priority: Gmail and Dialpad (most recent wins), then sheet fallback.
    Both Gmail and Dialpad caches are pre-populated before this function is called.
    """
    from integrations.dialpad import get_dialpad_contact_date
    contact_map: dict = {}
    for job in sheet_jobs:
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "")
        customer_phone = (job.get("customer_phone") or "").strip()

        history = get_email_history(client_name, pm_name)
        gmail_date = history.last_sent_at if history and history.last_sent_at else None

        dialpad_date = get_dialpad_contact_date(client_name, pm_name, customer_phone)

        candidates = [d for d in [gmail_date, dialpad_date] if d]
        if candidates:
            contact_date = max(candidates)
            if gmail_date and dialpad_date:
                source = "gmail" if gmail_date >= dialpad_date else "dialpad"
            elif gmail_date:
                source = "gmail"
            else:
                source = "dialpad"
            contact_map[client_name] = contact_date
            logger.info(f"Contact date for {client_name}: {contact_date} (source: {source})")
            continue

        sheet_date = parse_latest_date(job.get("sheet_last_contact", ""))
        if sheet_date:
            contact_map[client_name] = sheet_date
            logger.info(f"Contact date for {client_name}: {sheet_date} (source: sheet fallback)")
            continue

        contact_map[client_name] = None
        logger.info(f"Contact date for {client_name}: none (no gmail, dialpad, or sheet data)")
    return contact_map


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
    run_start = datetime.now(timezone.utc)

    from db.state_store import get_pm_list
    pm_config = get_pm_list()

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

    # ── Step 2c: PM↔customer contact history (Gmail + Dialpad) ──────────────
    _fetch_email_histories(sheet_jobs, pm_config)
    _fetch_dialpad_histories(sheet_jobs)
    contact_date_map = _build_contact_date_map(sheet_jobs)

    from db.state_store import get_all_active_jobs
    db_jobs = {j["client_name"]: j for j in get_all_active_jobs()}

    # Enrich sheet_jobs with comms_hold from DB so check functions can honour it
    for _job in sheet_jobs:
        _db = db_jobs.get(_job["client_name"], {})
        _job["comms_hold"] = _db.get("comms_hold", False)
        _job["comms_hold_reason"] = _db.get("comms_hold_reason", "")

    # ── Step 2d: Daily cadence compliance report (In Process jobs only) ──────
    cadence_flags = check_daily_cadence(sheet_jobs, contact_date_map)
    _checkpoint(run_id, f"Daily cadence: {len(cadence_flags)} In Process job(s) flagged")
    send_daily_cadence_report(cadence_flags)

    # ── Step 3: Run checks ───────────────────────────────────────────────────
    stale_flags          = check_stale_records(sheet_jobs, contact_date_map)
    missing_pm_flags     = check_missing_pm(sheet_jobs, contact_date_map)
    crew_flags           = check_unconfirmed_crew(sheet_jobs, contact_date_map)
    invoice_flags        = check_dropped_invoices(sheet_jobs, contact_date_map)
    approaching_jobs     = check_approaching_deadline(sheet_jobs, contact_date_map)

    _checkpoint(
        run_id,
        f"Checks: stale_record={len(stale_flags)}, missing_pm={len(missing_pm_flags)}, "
        f"unconfirmed_crew={len(crew_flags)}, dropped_invoice={len(invoice_flags)}, "
        f"approaching_deadline={len(approaching_jobs)}",
    )

    all_flags: list[dict] = []
    all_flags.extend(stale_flags)
    all_flags.extend(missing_pm_flags)
    all_flags.extend(crew_flags)
    all_flags.extend(invoice_flags)

    logger.info(f"OCA detected {len(all_flags)} flags total")
    _checkpoint(run_id, f"OCA detected {len(all_flags)} flags total")

    # ── Step 4: Process flags with deduplication + cooldown ─────────────────
    alerted = 0
    suppressed = 0
    alerted_flags: list[dict] = []

    # Group flags by job_id so cooldown checks are per-job
    flags_by_job: dict[str, list[dict]] = defaultdict(list)
    for flag in all_flags:
        flags_by_job[flag["job_id"]].append(flag)

    for job_id, job_flags in flags_by_job.items():
        active_flags: list[dict] = []
        for flag in job_flags:
            flag_type = flag["flag_type"]
            client_name = flag.get("client_name", job_id.split("|")[0])
            flag["hubspot_deal_id"] = db_jobs.get(client_name, {}).get("hubspot_deal_id")

            if flag_exists(job_id, flag_type):
                if should_alert_again(job_id, flag_type, cooldown_hours=24):
                    update_flag_alerted(job_id, flag_type)
                    active_flags.append(flag)
                    alerted += 1
                else:
                    logger.info(f"OCA suppressed (cooldown) {flag_type} for {job_id}")
                    suppressed += 1
            else:
                create_flag(job_id, flag_type, flag.get("details", ""), flag.get("urgency", ""))
                active_flags.append(flag)
                alerted += 1

        alerted_flags.extend(active_flags)

    # ── Step 4b: Approaching deadline — collect for PDF, skip individual sends ──
    to_notify_approaching: list[dict] = []
    for job in approaching_jobs:
        job_id = job["job_id"]
        flag_type = "approaching_deadline"
        if flag_exists(job_id, flag_type):
            if should_alert_again(job_id, flag_type, cooldown_hours=23):
                update_flag_alerted(job_id, flag_type)
                to_notify_approaching.append(job)
            else:
                logger.info(f"OCA suppressed approaching_deadline for {job_id} (cooldown)")
        else:
            create_flag(job_id, flag_type, f"PM last contacted customer {job.get('days_since_contact', 6)} days ago", "warning")
            to_notify_approaching.append(job)

    if to_notify_approaching:
        for job in to_notify_approaching:
            alerted_flags.append({
                "client_name":       job["client_name"],
                "pm_name":           job["pm_name"],
                "flag_type":         "approaching_deadline",
                "urgency":           "warning",
                "details":           f"PM last contacted customer {job.get('days_since_contact', 6)} days ago — Casey backstop triggers tomorrow",
                "last_contact_date": job.get("last_contact_date", ""),
                "hubspot_deal_id":   None,
            })
        _checkpoint(run_id, f"Approaching deadline included in report: {len(to_notify_approaching)} jobs")

    # ── Step 5: Auto-resolve flags ───────────────────────────────────────────
    for flag_type in _ALL_FLAG_TYPES:
        flagged_ids = [f["job_id"] for f in all_flags if f["flag_type"] == flag_type]
        if flag_type == "approaching_deadline":
            flagged_ids = [j["job_id"] for j in approaching_jobs]
        resolve_flags_not_in(flagged_ids, flag_type)

    logger.info(f"OCA run complete — alerted: {alerted}, suppressed: {suppressed}")

    run_summary = f"{len(all_flags)} flags detected, {alerted} alerted, {suppressed} suppressed"
    _checkpoint(run_id, f"OCA run complete — {run_summary}")

    # ── End-of-run PDF report ────────────────────────────────────────────────
    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    m, s = divmod(int(elapsed), 60)
    h, m = divmod(m, 60)
    run_duration_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")
    today_str = date.today().isoformat()
    n_issues = len(alerted_flags)
    try:
        from integrations.pdf_report import generate_pdf_report, send_pdf_report
        pdf_bytes = generate_pdf_report(
            agent="OCA",
            flags=alerted_flags,
            run_summary=run_summary,
            run_duration=run_duration_str,
        )
        if pdf_bytes:
            send_pdf_report(
                agent="OCA",
                pdf_bytes=pdf_bytes,
                slack_channel=cfg("slack_urgentmatters_channel") or "urgentmatters",
                slack_caption=f"OCA Run Report — {today_str} — {n_issues} issue{'s' if n_issues != 1 else ''} detected. See attached PDF.",
                email_to=cfg("chris_notification_email") or "chris.s@bluecollarscholars.net",
                email_from=cfg("notification_sender_email") or "",
                email_subject=f"OCA Run Report — {today_str} — {n_issues} issue{'s' if n_issues != 1 else ''} detected",
            )
    except Exception as e_pdf:
        logger.error(f"OCA PDF report failed: {e_pdf}")

    # ── Plain-text run summary to #urgentmatters ─────────────────────────────
    urgentmatters = cfg("slack_urgentmatters_channel") or "urgentmatters"
    send_message(urgentmatters, f"OCA Run Complete — {run_summary}")

    return run_summary


if __name__ == "__main__":
    run()
