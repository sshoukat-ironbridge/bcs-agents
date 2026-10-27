"""Classifies whether a customer's inbound email has been answered within
the response window. Pure function over plain message dicts; no DB access.

A response counts when, after the inbound message's received_at and within
`window_hours` consecutive calendar hours, there is either:
  - an outbound email in the same thread to that customer, or
  - a connected outbound call with that customer (duration > 0) — a missed
    or unanswered call does NOT count.
SMS counts only when sms_counts_as_response is True (default False).
Automated inbound mail is excluded entirely (never watched).
"""
from datetime import datetime, timedelta
from typing import Any

DEFAULT_WINDOW_HOURS = 24

STATUSES = ["waiting", "responded", "overdue", "excluded"]


def due_at(received_at: datetime, window_hours: int = DEFAULT_WINDOW_HOURS) -> datetime:
    return received_at + timedelta(hours=window_hours)


def _is_valid_response(inbound: dict, candidate: dict, sms_counts_as_response: bool) -> bool:
    if candidate.get("direction") != "outbound":
        return False

    source = candidate.get("source")
    if source == "gmail":
        return candidate.get("thread_id") == inbound.get("thread_id") and bool(inbound.get("thread_id"))

    if source == "dialpad_call":
        return (
            candidate.get("customer_phone") == inbound.get("customer_phone")
            and bool(inbound.get("customer_phone"))
            and (candidate.get("duration_ms") or 0) > 0
        )

    if source == "dialpad_sms":
        return (
            sms_counts_as_response
            and candidate.get("customer_phone") == inbound.get("customer_phone")
            and bool(inbound.get("customer_phone"))
        )

    return False


def classify_response(
    inbound: dict[str, Any],
    candidate_responses: list[dict[str, Any]],
    now: datetime,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    sms_counts_as_response: bool = False,
) -> str:
    """inbound: {"received_at", "thread_id", "customer_phone", "is_automated"}.
    candidate_responses: messages that might answer it, each shaped like
        a customer_messages row: {"source", "direction", "occurred_at",
        "thread_id", "customer_phone", "duration_ms"}.
    Returns one of STATUSES.
    """
    if inbound.get("is_automated"):
        return "excluded"

    received_at = inbound["received_at"]
    deadline = due_at(received_at, window_hours)

    for candidate in candidate_responses:
        occurred_at = candidate.get("occurred_at")
        if occurred_at is None or occurred_at <= received_at or occurred_at > deadline:
            continue
        if _is_valid_response(inbound, candidate, sms_counts_as_response):
            return "responded"

    if now > deadline:
        return "overdue"
    return "waiting"
