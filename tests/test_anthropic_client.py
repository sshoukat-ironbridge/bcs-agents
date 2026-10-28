from unittest.mock import patch, MagicMock

import pytest

from integrations.anthropic_client import (
    call_with_tool, extract_tool_input, extract_usage, api_key_configured, AnthropicClientError,
)


def _resp(status_code, json_body=None):
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_body or {}
    return m


TOOL_SCHEMA = {"type": "object", "properties": {"sentiment": {"type": "string"}}, "required": ["sentiment"]}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")


def test_api_key_configured_true_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert api_key_configured() is True


def test_api_key_configured_false_when_unset(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert api_key_configured() is False


def test_no_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AnthropicClientError):
        call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)


def test_successful_call_returns_response():
    body = {"model": "claude-sonnet-5-5", "id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 5},
             "content": [{"type": "tool_use", "name": "tool", "input": {"sentiment": "neutral"}}]}
    with patch("requests.post", return_value=_resp(200, body)):
        result = call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)
    assert result == body


def test_request_sends_correct_headers_and_tool_choice():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _resp(200, {"usage": {}, "content": []})

    with patch("requests.post", side_effect=fake_post):
        call_with_tool("my-model", 500, "system text", [{"role": "user", "content": "hi"}],
                        "my_tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "fake-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    # tool_choice is "auto", not forced - forced tool_choice 400s on
    # current-generation models including the default casey_model.
    assert captured["json"]["tool_choice"] == {"type": "auto"}
    assert captured["json"]["tools"][0]["name"] == "my_tool"
    assert captured["json"]["tools"][0]["strict"] is True
    assert captured["json"]["model"] == "my-model"
    assert captured["json"]["max_tokens"] == 500


def test_temperature_is_sent_when_given():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _resp(200, {"usage": {}, "content": []})

    with patch("requests.post", side_effect=fake_post):
        call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, temperature=0, _sleep=lambda s: None)
    assert captured["json"]["temperature"] == 0


def test_temperature_omitted_when_not_given():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _resp(200, {"usage": {}, "content": []})

    with patch("requests.post", side_effect=fake_post):
        call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)
    assert "temperature" not in captured["json"]


def test_429_is_retried_then_succeeds():
    responses = [_resp(429), _resp(200, {"usage": {}, "content": []})]
    sleeps = []
    with patch("requests.post", side_effect=responses):
        call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=sleeps.append)
    assert sleeps == [2]


def test_5xx_is_retried():
    responses = [_resp(503), _resp(200, {"usage": {}, "content": []})]
    with patch("requests.post", side_effect=responses):
        result = call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)
    assert result["content"] == []


def test_exhausted_retries_raises():
    with patch("requests.post", return_value=_resp(503)):
        with pytest.raises(AnthropicClientError):
            call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)


def test_non_retryable_status_fails_immediately():
    with patch("requests.post", return_value=_resp(400)) as mock_post:
        with pytest.raises(AnthropicClientError):
            call_with_tool("model", 100, "sys", [], "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)
    assert mock_post.call_count == 1


def test_extract_tool_input():
    response = {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "t", "input": {"a": 1}}]}
    assert extract_tool_input(response, "t") == {"a": 1}
    assert extract_tool_input(response, "missing") is None


def test_extract_usage():
    response = {"usage": {"input_tokens": 100, "output_tokens": 20}}
    assert extract_usage(response) == {"input_tokens": 100, "output_tokens": 20}
    assert extract_usage({}) == {"input_tokens": 0, "output_tokens": 0}


def test_log_messages_never_contain_system_or_message_text(caplog):
    import logging
    body = {"usage": {"input_tokens": 1, "output_tokens": 1}, "content": []}
    with patch("requests.post", return_value=_resp(200, body)), caplog.at_level(logging.INFO):
        call_with_tool("model", 100, "SECRET SYSTEM PROMPT", [{"role": "user", "content": "SECRET CUSTOMER TEXT"}],
                        "tool", "desc", TOOL_SCHEMA, _sleep=lambda s: None)
    for record in caplog.records:
        assert "SECRET SYSTEM PROMPT" not in record.message
        assert "SECRET CUSTOMER TEXT" not in record.message
