"""Plain-requests Anthropic Messages API client. No SDK dependency.

Never logs message/system text — only model, token usage, and response
ids. Uses forced tool use (tool_choice pinned to one tool) so the model's
output is a structured JSON object matching the given input_schema.
"""
import os
import time
from typing import Any

import requests

from utils.logger import get_logger

logger = get_logger("integrations.anthropic_client")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
REQUEST_TIMEOUT_SECONDS = 60
MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (2, 5)


class AnthropicClientError(Exception):
    def __init__(self, message: str, status_code: "int | None" = None):
        super().__init__(message)
        self.status_code = status_code


def api_key_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def call_with_tool(
    model: str,
    max_tokens: int,
    system: str,
    messages: list[dict[str, Any]],
    tool_name: str,
    tool_description: str,
    input_schema: dict[str, Any],
    _sleep=time.sleep,
) -> dict[str, Any]:
    """POSTs to /v1/messages so Anthropic returns one tool_use block shaped
    by input_schema. Returns the full parsed response JSON (use
    extract_tool_input() to pull the structured output out of it).
    Retries 429/5xx up to MAX_ATTEMPTS with backoff; any other error
    fails immediately.

    tool_choice is "auto", not forced to this tool: forced tool_choice
    (type "tool"/"any") returns a 400 on current-generation models,
    including the default casey_model (claude-sonnet-5-5) — confirmed
    against Anthropic's own current API docs, not assumed. The caller's
    system prompt must explicitly instruct the model to always respond
    by calling `tool_name`; `strict: true` on the tool (requiring
    `additionalProperties: false` in input_schema) keeps the structured-
    output guarantee the forced form would have given. See
    docs/casey_foundation.md for this correction."""
    api_key = _api_key()
    if not api_key:
        raise AnthropicClientError("ANTHROPIC_API_KEY is not set")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": [{"name": tool_name, "description": tool_description, "input_schema": input_schema, "strict": True}],
        "tool_choice": {"type": "auto"},
        # Classification-shaped task over a single customer message: low
        # effort is the documented recommendation for this workload shape
        # and keeps reasoning-token cost down.
        "output_config": {"effort": "low"},
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            if attempt < MAX_ATTEMPTS:
                delay = _RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(f"Anthropic request error (attempt {attempt}/{MAX_ATTEMPTS}), retrying in {delay}s")
                _sleep(delay)
                continue
            raise AnthropicClientError(f"Anthropic request failed after {MAX_ATTEMPTS} attempts: {type(e).__name__}") from e

        if resp.status_code == 200:
            data = resp.json()
            usage = data.get("usage", {})
            logger.info(
                f"Anthropic call ok: model={data.get('model')}, id={data.get('id')}, "
                f"input_tokens={usage.get('input_tokens')}, output_tokens={usage.get('output_tokens')}"
            )
            return data

        if _is_retryable_status(resp.status_code) and attempt < MAX_ATTEMPTS:
            delay = _RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(f"Anthropic API returned {resp.status_code} (attempt {attempt}/{MAX_ATTEMPTS}), retrying in {delay}s")
            _sleep(delay)
            continue

        raise AnthropicClientError(f"Anthropic API returned {resp.status_code}", status_code=resp.status_code)

    raise AnthropicClientError(f"Anthropic API failed after {MAX_ATTEMPTS} attempts")


def extract_tool_input(response: dict[str, Any], tool_name: str) -> "dict | None":
    """Pulls the structured tool_use input out of a /v1/messages response."""
    for block in response.get("content", []) or []:
        if block.get("type") == "tool_use" and block.get("name") == tool_name:
            return block.get("input")
    return None


def extract_usage(response: dict[str, Any]) -> dict[str, int]:
    usage = response.get("usage", {}) or {}
    return {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}
