import re
from datetime import date, datetime, timedelta, timezone

from utils.logger import get_logger

logger = get_logger("integrations.dialpad")


def _clean_phone(phone: str) -> str:
    """Return last 10 digits of a phone string for comparison."""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


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


def fetch_dialpad_contact_history(pm_name: str, customer_phone: str) -> "dict | None":
    """Fetch call history from Dialpad for a PM↔customer phone pair.

    Returns {"last_contact_date": date, "last_contact_type": str, "fetched_at": datetime}
    or None if nothing found or credentials are missing.
    """
    import requests

    if not customer_phone or not customer_phone.strip():
        return None

    cleaned_phone = _clean_phone(customer_phone)
    if not cleaned_phone:
        return None

    user_id = get_dialpad_user_id(pm_name)
    if not user_id:
        logger.info(f"Dialpad: no user_id for PM '{pm_name}' — skipping")
        return None

    api_key = _api_key()
    if not api_key:
        logger.info("Dialpad: api_key not configured — skipping")
        return None

    base_url = _base_url()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    best_date: "date | None" = None

    try:
        params = {"target_id": user_id, "target_type": "user", "limit": 50}
        resp = requests.get(
            f"{base_url}/call",
            headers=headers,
            params=params,
            timeout=30,
        )
        logger.info(
            f"Dialpad /call request for {pm_name}: "
            f"target_id={user_id} target_type=user — "
            f"status={resp.status_code} body={resp.text[:200]}"
        )
        if resp.status_code == 401:
            logger.error("Dialpad: invalid API key (401)")
            return None
        if resp.status_code != 200:
            logger.warning(f"Dialpad /call returned {resp.status_code} for PM '{pm_name}'")
            return None

        items = resp.json().get("items") or []
        for item in items:
            # Only outbound calls count as PM-initiated contact
            if item.get("direction") != "outbound":
                continue

            # Match against the three phone fields present in Dialpad call records
            candidates = []
            if item.get("external_number"):
                candidates.append(_clean_phone(str(item["external_number"])))
            contact = item.get("contact") or {}
            if contact.get("phone"):
                candidates.append(_clean_phone(str(contact["phone"])))
            target = item.get("target") or {}
            if target.get("phone"):
                candidates.append(_clean_phone(str(target["phone"])))

            if not any(
                c and (c == cleaned_phone or c.endswith(cleaned_phone) or cleaned_phone.endswith(c))
                for c in candidates
            ):
                continue

            ts = (item.get("date_started") or item.get("started_at")
                  or item.get("date_created") or item.get("created_at"))
            if not ts:
                continue
            try:
                contact_date = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
            except Exception as e:
                logger.error(
                    f"Dialpad date parse failed for call_id={item.get('call_id')}: "
                    f"{type(e).__name__}: {e}"
                )
                continue
            if best_date is None or contact_date > best_date:
                best_date = contact_date

    except requests.RequestException as e:
        logger.warning(f"Dialpad /call request error for PM '{pm_name}': {e}")
    except Exception as e:
        logger.error(f"Dialpad fetch failed for {pm_name} / {customer_phone}: {type(e).__name__}: {e}")
        return None

    if best_date is None:
        return None

    return {
        "last_contact_date": best_date,
        "last_contact_type": "call",
        "fetched_at": datetime.now(timezone.utc),
    }


def get_dialpad_contact_date(client_name: str, pm_name: str, customer_phone: str) -> "date | None":
    """Return cached or freshly-fetched last Dialpad contact date for a PM↔client pair.

    Uses a 23-hour cache in the dialpad_contact_history table. Never raises.
    """
    try:
        from db.state_store import get_dialpad_history, upsert_dialpad_history

        cached = get_dialpad_history(client_name, pm_name)
        if cached and cached.fetched_at:
            fetched_at = cached.fetched_at
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            if fetched_at >= datetime.now(timezone.utc) - timedelta(hours=23):
                return cached.last_contact_date

        result = fetch_dialpad_contact_history(pm_name, customer_phone)
        upsert_dialpad_history(
            client_name=client_name,
            pm_name=pm_name,
            customer_phone=customer_phone,
            last_contact_date=result["last_contact_date"] if result else None,
            last_contact_type=result["last_contact_type"] if result else None,
        )
        return result["last_contact_date"] if result else None

    except Exception as e:
        logger.error(f"get_dialpad_contact_date({client_name}): {e}")
        return None
