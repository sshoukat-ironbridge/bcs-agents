from datetime import date
from typing import Any

from integrations.sheets import parse_latest_date, is_valid_pm
from utils.logger import get_logger
from utils.timeutil import today_ny

logger = get_logger("oca.checks")


def _get_pm_config() -> list:
    try:
        from db.state_store import get_pm_list
        return get_pm_list()
    except Exception as e:
        logger.warning(f"Could not load pm_config from DB: {e}")
        return []


def _pm_email(pm_name: str) -> str:
    for pm in _get_pm_config():
        if pm.get("full_name") == pm_name:
            return pm.get("email", "")
    return ""


def _flag(client_name: str, pm_name: str, flag_type: str, details: str, urgency: str, last_contact_date: str = "") -> dict[str, Any]:
    return {
        "job_id": f"{client_name}|{pm_name}",
        "flag_type": flag_type,
        "details": details,
        "urgency": urgency,
        "client_name": client_name,
        "pm_name": pm_name,
        "pm_email": _pm_email(pm_name),
        "last_contact_date": last_contact_date,
    }


def check_missing_pm(sheet_jobs: list, contact_date_map: "dict | None" = None) -> list[dict[str, Any]]:
    """Flag jobs with no recognised PM that start within 14 days."""
    contact_date_map = contact_date_map or {}
    today = today_ny()
    pm_config = _get_pm_config()
    flags = []
    for job in sheet_jobs:
        if job.get("comms_hold"):
            logger.info(f"HOLD SKIP {job.get('client_name')} — comms hold active, reason: {job.get('comms_hold_reason')}")
            continue
        client_name = job["client_name"]
        pm_name = job.get("pm_name", "").strip()

        if is_valid_pm(pm_name, pm_config):
            continue

        # Find the earliest upcoming start date within 14 days
        use_date: date | None = None
        for raw_field in ("start_date", "estimated_start_window"):
            raw = job.get(raw_field, "")
            try:
                d = parse_latest_date(raw)
            except Exception:
                continue
            if d is None:
                continue
            days_away = (d - today).days
            if 0 <= days_away <= 14:
                if use_date is None or d < use_date:
                    use_date = d

        if use_date is None:
            continue

        last_contact = contact_date_map.get(client_name)
        flags.append(_flag(
            client_name, pm_name,
            "missing_pm",
            f"No PM assigned, job starts {use_date}",
            "urgent",
            last_contact_date=str(last_contact) if last_contact else "",
        ))
    logger.info(f"check_missing_pm: {len(flags)} flags")
    return flags


def classify_pm_issues(sheet_jobs: list, pm_config: "list | None" = None) -> dict[str, Any]:
    """Classify To Start / In Process jobs (not on comms_hold) by PM assignment problem.

    Reported regardless of start date — unlike check_missing_pm, which only
    flags jobs starting within 14 days for the oca_flags dedup system. This
    is a pure function over the current sheet job rows for the daily
    checklist, so it's a fresh snapshot every run, not sourced from
    oca_flags. check_missing_pm's own flag code path is untouched.

    "raw_name" is job["pm_name"] as resolved by sheets.py's _row_to_job —
    i.e. after normalize_pm_name()'s alias mapping, since the sheet's
    original unmapped cell text isn't carried on the job dict.

    Returns:
        {
            "blank": [{"client_name", "job_type"}, ...],
            "unrecognized": [{"raw_name", "job_count", "clients": [client_name, ...]}, ...],
        }
    """
    pm_config = pm_config if pm_config is not None else _get_pm_config()
    blank: list[dict[str, Any]] = []
    unrecognized_by_name: dict[str, list[str]] = {}

    for job in sheet_jobs:
        if job.get("sheet_tab") not in ("to_start", "in_process"):
            continue
        if job.get("comms_hold"):
            continue

        client_name = job.get("client_name", "")
        pm_name = (job.get("pm_name") or "").strip()

        if not pm_name:
            blank.append({"client_name": client_name, "job_type": job.get("job_type", "")})
            continue

        if not is_valid_pm(pm_name, pm_config):
            unrecognized_by_name.setdefault(pm_name, []).append(client_name)

    unrecognized = [
        {"raw_name": name, "job_count": len(clients), "clients": clients}
        for name, clients in unrecognized_by_name.items()
    ]
    return {"blank": blank, "unrecognized": unrecognized}


DEFAULT_DEADLINE_WARNING_DAYS = 7


def check_deadline_to_start(sheet_jobs: list, today: date, warning_days: int = DEFAULT_DEADLINE_WARNING_DAYS) -> dict[str, Any]:
    """Classify To Start jobs (not on comms_hold) by Deadline to Start.

    Pure function over job dicts and an explicit `today` — no DB/clock
    access — so it's directly unit-testable. The caller resolves
    `warning_days` from app_config (oca_deadline_warning_days, default 7).

    - overdue: deadline is before `today` (days_until is negative)
    - soon: deadline is today or within the next `warning_days` days (inclusive)
    Jobs with a missing or unparseable deadline_to_start are not listed
    individually — only counted in no_deadline_count, for the checklist
    footer.

    Returns:
        {
            "overdue": [{"client_name", "job_type", "pm_name", "deadline", "days_until"}, ...],
            "soon": [same shape, days_until >= 0],
            "no_deadline_count": int,
        }
    """
    overdue: list[dict[str, Any]] = []
    soon: list[dict[str, Any]] = []
    no_deadline_count = 0

    for job in sheet_jobs:
        if job.get("sheet_tab") != "to_start":
            continue
        if job.get("comms_hold"):
            continue

        deadline = parse_latest_date(job.get("deadline_to_start", ""))
        if deadline is None:
            no_deadline_count += 1
            continue

        days_until = (deadline - today).days
        item = {
            "client_name": job.get("client_name", ""),
            "job_type": job.get("job_type", ""),
            "pm_name": job.get("pm_name") or "unassigned",
            "deadline": deadline,
            "days_until": days_until,
        }
        if days_until < 0:
            overdue.append(item)
        elif days_until <= warning_days:
            soon.append(item)

    return {"overdue": overdue, "soon": soon, "no_deadline_count": no_deadline_count}


