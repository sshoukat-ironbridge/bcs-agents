from datetime import datetime, timedelta, timezone

from agents.casey.ingest_windows import parse_cursor, next_window, paginate

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def test_parse_cursor_none_is_none():
    assert parse_cursor(None) is None
    assert parse_cursor("") is None


def test_parse_cursor_attaches_utc_when_naive():
    dt = parse_cursor("2026-10-20T09:00:00")
    assert dt.tzinfo == timezone.utc


def test_next_window_with_no_cursor_uses_default_lookback():
    window = next_window(None, NOW)
    assert window["end"] == NOW
    assert window["start"] == NOW - timedelta(hours=24)


def test_next_window_with_cursor_overlaps_by_default_minutes():
    cursor = "2026-10-27T10:00:00+00:00"
    window = next_window(cursor, NOW)
    assert window["end"] == NOW
    assert window["start"] == datetime(2026, 10, 27, 9, 55, tzinfo=timezone.utc)


def test_next_window_custom_overlap_and_lookback():
    window = next_window(None, NOW, overlap_minutes=10, default_lookback_hours=2)
    assert window["start"] == NOW - timedelta(hours=2)


def test_paginate_stops_when_next_cursor_is_none():
    def fetch_page(cursor):
        if cursor is None:
            return (["a", "b"], "page2")
        if cursor == "page2":
            return (["c"], None)
        raise AssertionError("should not be called again")

    result = paginate(fetch_page)
    assert result["items"] == ["a", "b", "c"]
    assert result["pages_fetched"] == 2
    assert result["hit_page_cap"] is False


def test_paginate_hits_hard_cap_on_runaway_pagination():
    calls = {"count": 0}

    def fetch_page(cursor):
        calls["count"] += 1
        return (["x"], f"page{calls['count']}")  # never returns None - would loop forever

    result = paginate(fetch_page, max_pages=5)
    assert result["pages_fetched"] == 5
    assert result["hit_page_cap"] is True
    assert len(result["items"]) == 5


def test_paginate_single_page():
    def fetch_page(cursor):
        return (["only"], None)

    result = paginate(fetch_page)
    assert result == {"items": ["only"], "pages_fetched": 1, "hit_page_cap": False}


def test_paginate_never_calls_fetch_page_itself_makes_no_network_call():
    # fetch_page is entirely caller-supplied; paginate just loops it.
    seen_cursors = []

    def fetch_page(cursor):
        seen_cursors.append(cursor)
        return ([], None)

    paginate(fetch_page, start_cursor="abc")
    assert seen_cursors == ["abc"]
