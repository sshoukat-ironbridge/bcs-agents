"""Read-only Dialpad call/transcript API client. Never logs the API key,
transcript text, or phone numbers — only counts, call IDs, and status
codes. See docs/casey_foundation.md for which fields are confirmed vs.
assumed from Dialpad's docs.
"""
import time
from typing import Any

import requests

from utils.logger import get_logger
from integrations.dialpad import _api_key, _base_url

logger = get_logger("integrations.dialpad_calls")

REQUEST_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (2, 5)
REQUEST_DELAY_SECONDS = 0.2  # small gap between successive requests
MAX_PAGES = 50


class DialpadApiError(Exception):
    """A Dialpad API call failed (after retries, for a retryable status;
    immediately, for anything else). status_code is None for a request
    that never got a response (timeout/connection error)."""

    def __init__(self, message: str, status_code: "int | None" = None):
        super().__init__(message)
        self.status_code = status_code


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _get(path: str, params: dict, _sleep=time.sleep) -> dict:
    api_key = _api_key()
    if not api_key:
        raise DialpadApiError("dialpad_api_key is not configured")

    url = f"{_base_url()}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            if attempt < MAX_ATTEMPTS:
                delay = _RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(f"Dialpad request error on {path} (attempt {attempt}/{MAX_ATTEMPTS}), retrying in {delay}s")
                _sleep(delay)
                continue
            raise DialpadApiError(f"Dialpad request to {path} failed after {MAX_ATTEMPTS} attempts: {type(e).__name__}") from e

        if resp.status_code == 200:
            _sleep(REQUEST_DELAY_SECONDS)
            return resp.json()

        if _is_retryable_status(resp.status_code) and attempt < MAX_ATTEMPTS:
            delay = _RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(f"Dialpad API {path} returned {resp.status_code} (attempt {attempt}/{MAX_ATTEMPTS}), retrying in {delay}s")
            _sleep(delay)
            continue

        raise DialpadApiError(f"Dialpad API {path} returned {resp.status_code}", status_code=resp.status_code)

    raise DialpadApiError(f"Dialpad API {path} failed after {MAX_ATTEMPTS} attempts")


def list_calls(user_id: str, start_ms: int, end_ms: int, max_pages: int = MAX_PAGES, _sleep=time.sleep) -> list[dict[str, Any]]:
    """Lists calls for the given Dialpad user_id with started_after=start_ms,
    started_before=end_ms (UTC epoch milliseconds, confirmed param names).
    Cursor-paginated (no server-side page-size param is documented) with a
    hard client-side page cap. Logs counts only, never call content."""
    calls: list[dict] = []
    cursor = None
    pages = 0

    while pages < max_pages:
        params = {
            "target_id": user_id,
            "target_type": "user",
            "started_after": start_ms,
            "started_before": end_ms,
        }
        if cursor:
            params["cursor"] = cursor
        data = _get("/call", params, _sleep=_sleep)
        items = data.get("items") or []
        calls.extend(items)
        pages += 1
        cursor = data.get("cursor")
        if not cursor:
            break

    logger.info(f"list_calls(user_id={user_id}): {len(calls)} call(s) across {pages} page(s)")
    if pages >= max_pages and cursor:
        logger.warning(f"list_calls(user_id={user_id}): hit page cap ({max_pages}) with more pages remaining")
    return calls


def get_transcript(call_id, _sleep=time.sleep) -> "dict | None":
    """Returns {"call_id": ..., "lines": [...]}, or None if the call has
    no transcript (404 — Dialpad's docs don't document a specific
    "not ready" response, so this is the best available signal). Any
    other error (auth, 5xx after retries, etc.) propagates as
    DialpadApiError rather than being silently treated as "no transcript".
    Logs the line count only, never transcript text."""
    try:
        data = _get(f"/transcripts/{call_id}", {}, _sleep=_sleep)
    except DialpadApiError as e:
        if e.status_code == 404:
            logger.info(f"get_transcript(call_id={call_id}): no transcript available (404)")
            return None
        raise

    line_count = len((data or {}).get("lines") or [])
    logger.info(f"get_transcript(call_id={call_id}): {line_count} line(s)")
    return data
