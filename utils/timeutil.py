from datetime import date, datetime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def now_ny() -> datetime:
    """Current wall-clock time in America/New_York."""
    return datetime.now(NY_TZ)


def today_ny() -> date:
    """Current calendar date in America/New_York."""
    return now_ny().date()
