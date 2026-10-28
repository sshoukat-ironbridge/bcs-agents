from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def now_ny() -> datetime:
    """Current wall-clock time in America/New_York."""
    return datetime.now(NY_TZ)


def today_ny() -> date:
    """Current calendar date in America/New_York."""
    return now_ny().date()


def format_ny(dt: "datetime | None") -> str:
    """Formats a datetime for display in America/New_York, e.g.
    "Oct 19, 2:40 PM". A naive datetime is treated as UTC (every
    timestamp this app stores is UTC-aware already; this is a defensive
    fallback, not an assumption to rely on)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(NY_TZ)
    return local.strftime("%b %-d, %-I:%M %p")
