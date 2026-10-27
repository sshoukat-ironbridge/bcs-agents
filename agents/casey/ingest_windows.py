"""Time-window and pagination helpers for Casey's ingestion agents.

No network/DB access — callers own the actual API calls; this module only
decides which window to fetch next and loops a caller-supplied page
fetcher with a hard cap.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

DEFAULT_OVERLAP_MINUTES = 5
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_MAX_PAGES = 50


def parse_cursor(cursor_value: "str | None") -> "datetime | None":
    if not cursor_value:
        return None
    dt = datetime.fromisoformat(cursor_value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def next_window(
    cursor_value: "str | None",
    now: datetime,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    default_lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict[str, datetime]:
    """Returns {"start": ..., "end": now} to fetch next.

    With no prior cursor, starts default_lookback_hours before now (first
    run). With a prior cursor, starts overlap_minutes before it, so an
    event that arrived right at the edge of the last window (e.g. a late
    webhook, clock skew) is re-fetched rather than missed — sources that
    dedupe on (source, external_id) make the overlap idempotent.
    """
    last = parse_cursor(cursor_value)
    start = (now - timedelta(hours=default_lookback_hours)) if last is None \
        else (last - timedelta(minutes=overlap_minutes))
    return {"start": start, "end": now}


def paginate(
    fetch_page: Callable[["str | None"], "tuple[list, str | None]"],
    start_cursor: "str | None" = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Loops fetch_page(page_cursor) -> (items, next_page_cursor) until
    next_page_cursor is None or max_pages is reached (hard cap, so a
    pagination bug or runaway feed can't loop forever). fetch_page is
    supplied by the caller and does the actual network call — this
    function makes none itself.

    Returns {"items": [...], "pages_fetched": int, "hit_page_cap": bool}.
    """
    items: list = []
    page_cursor = start_cursor
    pages_fetched = 0

    while pages_fetched < max_pages:
        page_items, next_page_cursor = fetch_page(page_cursor)
        items.extend(page_items)
        pages_fetched += 1
        if next_page_cursor is None:
            return {"items": items, "pages_fetched": pages_fetched, "hit_page_cap": False}
        page_cursor = next_page_cursor

    return {"items": items, "pages_fetched": pages_fetched, "hit_page_cap": True}
