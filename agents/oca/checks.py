from datetime import date
from typing import Any

from integrations.sheets import parse_latest_date, is_valid_pm
from utils.logger import get_logger

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
    today = date.today()
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


