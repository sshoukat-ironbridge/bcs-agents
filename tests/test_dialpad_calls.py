from unittest.mock import patch, MagicMock

import pytest

from integrations.dialpad_calls import list_calls, get_transcript, DialpadApiError


def _resp(status_code, json_body=None):
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_body or {}
    return m


@pytest.fixture(autouse=True)
def api_key():
    with patch("integrations.dialpad_calls._api_key", return_value="fake-key"), \
         patch("integrations.dialpad_calls._base_url", return_value="https://dialpad.test/api/v2"):
        yield


def test_list_calls_single_page():
    with patch("requests.get", return_value=_resp(200, {"items": [{"call_id": 1}, {"call_id": 2}], "cursor": None})) as mock_get:
        calls = list_calls("u1", 1000, 2000, _sleep=lambda s: None)
    assert [c["call_id"] for c in calls] == [1, 2]
    assert mock_get.call_count == 1


def test_list_calls_pagination_follows_cursor():
    page1 = _resp(200, {"items": [{"call_id": 1}], "cursor": "page2"})
    page2 = _resp(200, {"items": [{"call_id": 2}], "cursor": None})
    with patch("requests.get", side_effect=[page1, page2]) as mock_get:
        calls = list_calls("u1", 1000, 2000, _sleep=lambda s: None)
    assert [c["call_id"] for c in calls] == [1, 2]
    assert mock_get.call_count == 2


def test_list_calls_hits_hard_page_cap():
    def infinite_pages(*args, **kwargs):
        return _resp(200, {"items": [{"call_id": 1}], "cursor": "always_more"})

    with patch("requests.get", side_effect=infinite_pages) as mock_get:
        calls = list_calls("u1", 1000, 2000, max_pages=3, _sleep=lambda s: None)
    assert mock_get.call_count == 3
    assert len(calls) == 3


def test_429_is_retried_then_succeeds():
    sleeps = []
    responses = [_resp(429), _resp(200, {"items": [], "cursor": None})]
    with patch("requests.get", side_effect=responses):
        calls = list_calls("u1", 1000, 2000, _sleep=sleeps.append)
    assert calls == []
    assert sleeps[0] == 2  # first retry delay


def test_5xx_is_retried():
    responses = [_resp(503), _resp(200, {"items": [], "cursor": None})]
    with patch("requests.get", side_effect=responses):
        calls = list_calls("u1", 1000, 2000, _sleep=lambda s: None)
    assert calls == []


def test_exhausted_retries_raises():
    with patch("requests.get", return_value=_resp(503)):
        with pytest.raises(DialpadApiError):
            list_calls("u1", 1000, 2000, _sleep=lambda s: None)


def test_non_retryable_status_fails_immediately():
    with patch("requests.get", return_value=_resp(401)) as mock_get:
        with pytest.raises(DialpadApiError):
            list_calls("u1", 1000, 2000, _sleep=lambda s: None)
    assert mock_get.call_count == 1


def test_get_transcript_returns_lines():
    with patch("requests.get", return_value=_resp(200, {"call_id": 42, "lines": [{"content": "hi"}]})):
        result = get_transcript(42, _sleep=lambda s: None)
    assert result["call_id"] == 42
    assert len(result["lines"]) == 1


def test_get_transcript_404_returns_none():
    with patch("requests.get", return_value=_resp(404)):
        result = get_transcript(999, _sleep=lambda s: None)
    assert result is None


def test_get_transcript_non_404_error_propagates():
    with patch("requests.get", return_value=_resp(500)):
        with pytest.raises(DialpadApiError):
            get_transcript(999, _sleep=lambda s: None)


def test_no_api_key_raises():
    with patch("integrations.dialpad_calls._api_key", return_value=""):
        with pytest.raises(DialpadApiError):
            list_calls("u1", 1000, 2000, _sleep=lambda s: None)


def test_log_messages_never_contain_phone_or_text(caplog):
    import logging
    with patch("requests.get", return_value=_resp(200, {"items": [{"call_id": 1}], "cursor": None})), \
         caplog.at_level(logging.INFO):
        list_calls("u1", 1000, 2000, _sleep=lambda s: None)
    for record in caplog.records:
        assert "+1" not in record.message  # crude phone-number smell test
