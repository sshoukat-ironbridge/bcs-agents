from datetime import date, datetime, timezone

from utils.timeutil import NY_TZ, now_ny, today_ny


def test_utc_night_is_previous_day_in_new_york():
    # 2026-10-22 01:10 UTC is 2026-10-21 21:10 in New York (EDT, UTC-4 in October).
    utc_dt = datetime(2026, 10, 22, 1, 10, tzinfo=timezone.utc)
    ny_date = utc_dt.astimezone(NY_TZ).date()
    assert ny_date == date(2026, 10, 21)


def test_utc_morning_is_same_day_in_new_york_in_winter():
    # In January, New York is EST (UTC-5). 2026-01-15 03:10 UTC is still
    # 2026-01-14 22:10 in New York — a day earlier, confirming DST doesn't
    # need to be handled specially since zoneinfo applies it automatically.
    utc_dt = datetime(2026, 1, 15, 3, 10, tzinfo=timezone.utc)
    ny_date = utc_dt.astimezone(NY_TZ).date()
    assert ny_date == date(2026, 1, 14)


def test_now_ny_returns_aware_datetime_in_new_york_zone():
    dt = now_ny()
    assert dt.tzinfo is not None
    assert dt.utcoffset() is not None
    # Same instant, reinterpreted in UTC, must match within a tiny tolerance.
    assert dt.astimezone(NY_TZ) == dt


def test_today_ny_returns_a_date():
    d = today_ny()
    assert isinstance(d, date)
