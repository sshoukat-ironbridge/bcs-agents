from datetime import date

from integrations.sheets import parse_latest_date


def test_iso_format():
    assert parse_latest_date("2026-07-11") == date(2026, 7, 11)


def test_us_slash_two_digit_year():
    assert parse_latest_date("07/11/26") == date(2026, 7, 11)


def test_us_slash_four_digit_year():
    assert parse_latest_date("07/11/2026") == date(2026, 7, 11)


def test_month_day_only_uses_current_year():
    result = parse_latest_date("07/11")
    assert result.month == 7 and result.day == 11
    assert result.year == date.today().year


def test_month_dash_day_only_uses_current_year():
    result = parse_latest_date("07-11")
    assert result.month == 7 and result.day == 11


def test_multiline_picks_latest():
    value = "2026-05-15\n2026-05-29\n2026-06-09\n2026-06-14"
    assert parse_latest_date(value) == date(2026, 6, 14)


def test_empty_or_blank_returns_none():
    assert parse_latest_date("") is None
    assert parse_latest_date("   ") is None


def test_unparseable_returns_none():
    assert parse_latest_date("not a date") is None


def test_none_input_returns_none():
    assert parse_latest_date(None) is None
