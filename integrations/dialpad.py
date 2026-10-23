from datetime import date, datetime, timezone

from utils.logger import get_logger

logger = get_logger("integrations.dialpad")


def _api_key() -> str:
    try:
        from db.state_store import get_config
        return (get_config("dialpad_api_key") or "").strip()
    except Exception:
        return ""


def _base_url() -> str:
    try:
        from db.state_store import get_config
        url = (get_config("dialpad_api_base_url") or "").strip()
        return url.rstrip("/") if url else "https://dialpad.com/api/v2"
    except Exception:
        return "https://dialpad.com/api/v2"


def get_dialpad_user_id(pm_name: str) -> "str | None":
    try:
        from db.state_store import find_pm
        pm = find_pm(pm_name)
        if pm:
            uid = (pm.get("dialpad_user_id") or "").strip()
            return uid or None
    except Exception as e:
        logger.error(f"get_dialpad_user_id({pm_name}): {e}")
    return None


def _parse_contact_date(ts) -> "date | None":
    """Parse a Dialpad timestamp to a date. Dialpad always returns millisecond epochs."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
    except Exception:
        return None


