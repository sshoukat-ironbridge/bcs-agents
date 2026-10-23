import ssl
from unittest.mock import MagicMock

import pytest

from integrations import sheets
from integrations.sheets import get_all_rows, SheetFetchError


def _service_raising(side_effects):
    """A fake Sheets API service whose .execute() yields side_effects in order
    (an exception instance is raised; anything else is returned)."""
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.side_effect = side_effects
    return service


def test_retries_transient_ssl_error_then_succeeds(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    service = _service_raising([
        ssl.SSLError("transient 1"),
        ssl.SSLError("transient 2"),
        {"values": [["h1", "h2"], ["a", "b"]]},
    ])
    monkeypatch.setattr(sheets, "_get_service", lambda: service)

    sleeps = []
    rows = get_all_rows("sheet_id", "Tab", header_row=1, _sleep=sleeps.append)

    assert rows == [{"h1": "a", "h2": "b"}]
    # Two retries before success -> backoff delays 2s then 5s.
    assert sleeps == [2, 5]


def test_raises_clear_error_after_exhausting_all_retries(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    service = _service_raising([ssl.SSLError("always fails")] * 10)
    monkeypatch.setattr(sheets, "_get_service", lambda: service)

    sleeps = []
    with pytest.raises(SheetFetchError):
        get_all_rows("sheet_id", "Tab", header_row=1, _sleep=sleeps.append)

    # 3 retries -> delays 2s, 5s, 10s (no sleep after the final failed attempt).
    assert sleeps == [2, 5, 10]


def test_non_retryable_error_raises_immediately_without_sleeping(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    service = _service_raising([RuntimeError("bad request, retrying won't help")])
    monkeypatch.setattr(sheets, "_get_service", lambda: service)

    sleeps = []
    with pytest.raises(SheetFetchError):
        get_all_rows("sheet_id", "Tab", header_row=1, _sleep=sleeps.append)

    assert sleeps == []  # failed on first attempt, no retry


def test_timeout_error_is_retryable(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    service = _service_raising([
        TimeoutError("timed out"),
        {"values": [["h1"], ["a"]]},
    ])
    monkeypatch.setattr(sheets, "_get_service", lambda: service)

    sleeps = []
    rows = get_all_rows("sheet_id", "Tab", header_row=1, _sleep=sleeps.append)
    assert rows == [{"h1": "a"}]
    assert sleeps == [2]


def test_empty_tab_returns_empty_list_not_an_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    service = _service_raising([{"values": []}])
    monkeypatch.setattr(sheets, "_get_service", lambda: service)

    rows = get_all_rows("sheet_id", "Tab", header_row=2)
    assert rows == []
