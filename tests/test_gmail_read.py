from unittest.mock import patch, MagicMock

import pytest

from integrations.gmail_read import list_messages, get_message, get_thread, GmailReadError


class _FakeHttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.resp = MagicMock(status=status_code)


def _fake_service(list_pages=None, get_result=None, thread_result=None, raise_seq=None):
    """Builds a MagicMock standing in for the googleapiclient Gmail
    service object, matching the `.users().messages().list()/.get()`
    and `.users().threads().get()` chains.

    raise_seq, if given, is consumed one entry per list() call attempt
    (including failed ones); a None entry means "succeed this attempt
    and return the next list_pages page" instead of raising."""
    service = MagicMock()
    calls = {"list_attempts": 0, "pages_returned": 0, "get": 0, "thread_get": 0}

    def list_execute(num_retries=0):
        idx = calls["list_attempts"]
        calls["list_attempts"] += 1
        if raise_seq and idx < len(raise_seq) and raise_seq[idx] is not None:
            raise raise_seq[idx]
        page = list_pages[calls["pages_returned"]] if list_pages else {}
        calls["pages_returned"] += 1
        return page

    def get_execute(num_retries=0):
        calls["get"] += 1
        return get_result

    def thread_get_execute(num_retries=0):
        calls["thread_get"] += 1
        return thread_result

    list_mock = MagicMock()
    list_mock.execute.side_effect = list_execute
    service.users.return_value.messages.return_value.list.return_value = list_mock

    get_mock = MagicMock()
    get_mock.execute.side_effect = get_execute
    service.users.return_value.messages.return_value.get.return_value = get_mock

    thread_mock = MagicMock()
    thread_mock.execute.side_effect = thread_get_execute
    service.users.return_value.threads.return_value.get.return_value = thread_mock

    return service


@pytest.fixture(autouse=True)
def sa_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')


def test_list_messages_single_page():
    service = _fake_service(list_pages=[{"messages": [{"id": "1", "threadId": "t1"}]}])
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = list_messages("mb@example.com", "after:1 before:2", _sleep=lambda s: None)
    assert result == [{"id": "1", "threadId": "t1"}]


def test_list_messages_pagination():
    pages = [
        {"messages": [{"id": "1"}], "nextPageToken": "p2"},
        {"messages": [{"id": "2"}]},
    ]
    service = _fake_service(list_pages=pages)
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = list_messages("mb@example.com", "q", _sleep=lambda s: None)
    assert [m["id"] for m in result] == ["1", "2"]


def test_list_messages_respects_max_results_cap():
    pages = [
        {"messages": [{"id": "1"}, {"id": "2"}], "nextPageToken": "p2"},
        {"messages": [{"id": "3"}, {"id": "4"}]},
    ]
    service = _fake_service(list_pages=pages)
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = list_messages("mb@example.com", "q", max_results=3, _sleep=lambda s: None)
    assert len(result) == 3


def test_429_is_retried_then_succeeds():
    pages = [{"messages": [{"id": "1"}]}]
    service = _fake_service(list_pages=pages, raise_seq=[_FakeHttpError(429), None])
    sleeps = []
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = list_messages("mb@example.com", "q", _sleep=sleeps.append)
    assert result == [{"id": "1"}]
    assert sleeps == [2]


def test_5xx_is_retried():
    pages = [{"messages": []}]
    service = _fake_service(list_pages=pages, raise_seq=[_FakeHttpError(503), None])
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = list_messages("mb@example.com", "q", _sleep=lambda s: None)
    assert result == []


def test_non_retryable_error_propagates_immediately():
    service = _fake_service(raise_seq=[_FakeHttpError(400)])
    with patch("integrations.gmail_read._get_service", return_value=service):
        with pytest.raises(_FakeHttpError):
            list_messages("mb@example.com", "q", _sleep=lambda s: None)
    assert service.users.return_value.messages.return_value.list.call_count == 1


def test_get_message_returns_full_payload():
    service = _fake_service(get_result={"id": "1", "payload": {"headers": []}})
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = get_message("mb@example.com", "1", _sleep=lambda s: None)
    assert result["id"] == "1"


def test_get_thread_returns_full_payload():
    service = _fake_service(thread_result={"id": "t1", "messages": []})
    with patch("integrations.gmail_read._get_service", return_value=service):
        result = get_thread("mb@example.com", "t1", _sleep=lambda s: None)
    assert result["id"] == "t1"


def test_no_service_account_raises():
    import os
    old = os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
    try:
        with pytest.raises(GmailReadError):
            list_messages("mb@example.com", "q", _sleep=lambda s: None)
    finally:
        if old is not None:
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = old


def test_module_never_calls_send_or_modify_methods():
    import inspect
    import integrations.gmail_read as module
    source = inspect.getsource(module)
    for forbidden in ("messages().send", "messages().modify", "messages().delete",
                       "messages().batchModify", "messages().batchDelete",
                       "labels().create", "labels().update", "labels().delete"):
        assert forbidden not in source


def test_log_messages_never_contain_subject_or_body(caplog):
    import logging
    service = _fake_service(get_result={"id": "1", "payload": {"headers": [
        {"name": "Subject", "value": "SECRET SUBJECT TEXT"}
    ]}, "snippet": "SECRET BODY SNIPPET"})
    with patch("integrations.gmail_read._get_service", return_value=service), \
         caplog.at_level(logging.INFO):
        get_message("mb@example.com", "1", _sleep=lambda s: None)
    for record in caplog.records:
        assert "SECRET SUBJECT TEXT" not in record.message
        assert "SECRET BODY SNIPPET" not in record.message
