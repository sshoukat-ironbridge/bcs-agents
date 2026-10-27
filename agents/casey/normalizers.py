"""Converts raw provider payloads into customer_messages-shaped row dicts,
plus a pure contact-based job matcher. No network/DB access — callers
fetch the raw payload and pass it in here.
"""
import json
from datetime import datetime, timezone
from typing import Any

# Company domain used to classify Gmail message direction (outbound = we
# sent it). Matches the one address already hardcoded elsewhere in this
# repo (db/state_store.py's chris_notification_email default).
OUR_EMAIL_DOMAIN = "bluecollarscholars.net"

_AUTOMATED_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "mailer-daemon"}
_BCS_UPDATE_SUBJECT_PREFIX = "[BCS Update]"
_BULK_PRECEDENCE_VALUES = {"bulk", "list", "junk"}


# --- Dialpad call transcript -> customer_messages row ----------------------

def _parse_utc_naive(value: "str | None") -> "datetime | None":
    """Transcript line timestamps are UTC ISO without a timezone suffix
    (confirmed: https://developers.dialpad.com/reference/transcriptsget.md);
    attach UTC explicitly rather than treating them as local time."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_epoch_ms(value) -> "datetime | None":
    """The call object's date_started is an int64 ms-epoch timestamp
    (confirmed: https://developers.dialpad.com/reference/calllist.md) —
    NOT an ISO string like the transcript lines' "time" field. Caught by
    the Stage 5 live check against real Dialpad data."""
    if not value:
        return None
    return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)


def normalize_dialpad_transcript(call: dict, transcript: dict) -> "dict[str, Any] | None":
    """call: a Dialpad call object (call_id, direction, duration [ms],
    date_started [int64 ms epoch], contact {phone, email, type}) — field
    names confirmed directly against Dialpad's docs, not guessed.
    transcript: {"lines": [{"type": "transcript"|"moment", "user_id":
    str|None, "contact_id": str|None, "content": str, "time": UTC ISO,
    no tz}]}.

    Lines with user_id are the PM side, lines with contact_id are the
    customer side. "moment" lines (call events, not speech) are excluded
    from the text body but every line — transcript and moment alike —
    stays in raw_ref. A transcript with zero lines returns None (nothing
    to ingest): skip it rather than creating an empty row.
    """
    lines = transcript.get("lines") or []
    if not lines:
        return None

    text_parts = []
    for line in lines:
        if line.get("type") == "moment":
            continue
        if line.get("user_id"):
            speaker = "PM"
        elif line.get("contact_id"):
            speaker = "Customer"
        else:
            speaker = "Unknown"
        text_parts.append(f"{speaker}: {line.get('content', '')}")

    contact = call.get("contact") or {}
    return {
        "source": "dialpad_call",
        "external_id": str(call.get("call_id")),
        "direction": call.get("direction") or "inbound",
        "occurred_at": _parse_epoch_ms(call.get("date_started")),
        "customer_phone": contact.get("phone"),
        "customer_email": contact.get("email"),
        "body_text": "\n".join(text_parts),
        "duration_ms": call.get("duration") or 0,
        "is_automated": False,
        "raw_ref": json.dumps({"call": call, "transcript": transcript}),
    }


# --- Gmail message -> customer_messages row --------------------------------

def _header_map(headers: list) -> dict:
    return {h.get("name", "").lower(): h.get("value", "") for h in (headers or [])}


def _sender_address(sender: str) -> str:
    if "<" in sender and ">" in sender:
        return sender.split("<", 1)[1].split(">", 1)[0].strip()
    return sender.strip()


def _sender_domain(sender: str) -> str:
    addr = _sender_address(sender)
    return addr.rsplit("@", 1)[-1].strip().lower() if "@" in addr else ""


def _sender_local_part(sender: str) -> str:
    addr = _sender_address(sender)
    return addr.split("@", 1)[0].strip().lower() if "@" in addr else ""


def is_automated_email(headers: dict, subject: str, sender: str) -> bool:
    """True when any of: Auto-Submitted other than "no", bulk/list/junk
    Precedence, a List-Unsubscribe/List-Id header, X-Autoreply, a
    noreply-style sender local part, or a "[BCS Update]" subject prefix
    (Casey's own automated emails)."""
    auto_submitted = (headers.get("auto-submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if (headers.get("precedence") or "").strip().lower() in _BULK_PRECEDENCE_VALUES:
        return True
    if headers.get("list-unsubscribe") or headers.get("list-id"):
        return True
    if headers.get("x-autoreply"):
        return True
    if _sender_local_part(sender) in _AUTOMATED_LOCAL_PARTS:
        return True
    if subject.strip().startswith(_BCS_UPDATE_SUBJECT_PREFIX):
        return True
    return False


def normalize_gmail_message(message: dict) -> dict:
    """message: a Gmail API message resource {"id", "threadId", "payload":
    {"headers": [...]}, "snippet", "internalDate" (ms epoch, UTC)}."""
    headers = _header_map((message.get("payload") or {}).get("headers"))
    sender = headers.get("from", "")
    subject = headers.get("subject", "")

    internal_date = message.get("internalDate")
    occurred_at = (
        datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        if internal_date else None
    )

    return {
        "source": "gmail",
        "external_id": message.get("id"),
        "thread_id": message.get("threadId"),
        "direction": "outbound" if _sender_domain(sender) == OUR_EMAIL_DOMAIN else "inbound",
        "sender": sender,
        "subject": subject,
        "body_text": message.get("snippet", ""),
        "occurred_at": occurred_at,
        "is_automated": is_automated_email(headers, subject, sender),
        "raw_ref": json.dumps(message),
    }


# --- Dialpad SMS webhook payload -> customer_messages row -------------------
# Confirmed shape per https://developers.dialpad.com/docs/sms-events.md
# (fetched directly; not a guess): id, direction, created_date (ms epoch,
# UTC), from_number, to_number (list), text, contact {phone_number, name}
# — "contact" is always the external party regardless of direction.

def normalize_dialpad_sms(payload: dict) -> dict:
    """payload: the decoded Dialpad SMS webhook event body (decode/verify
    the signed JWT before calling this — see the webhook receiver)."""
    created_date = payload.get("created_date")
    occurred_at = (
        datetime.fromtimestamp(created_date / 1000, tz=timezone.utc)
        if created_date else None
    )
    contact = payload.get("contact") or {}

    return {
        "source": "dialpad_sms",
        "external_id": str(payload.get("id")),
        "direction": payload.get("direction") or "inbound",
        "occurred_at": occurred_at,
        "customer_phone": contact.get("phone_number"),
        "sender": payload.get("from_number"),
        "body_text": payload.get("text", ""),
        "is_automated": False,
        "raw_ref": json.dumps(payload),
    }


# --- Job matching: customer email first, then phone -------------------------

def _normalize_phone(phone: str) -> str:
    """Strips everything but digits, then keeps only the last 10 — so
    formatting differences (dashes/spaces/parens) AND a leading country
    code ("+1") don't cause two representations of the same US number to
    miss each other. A string with fewer than 10 digits is returned as-is
    (nothing to truncate)."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def build_contact_index(jobs: list[dict]) -> dict:
    """jobs: casey_active_jobs-shaped dicts (client_name, sheet_tab,
    primary_job_type, customer_email, customer_phone). Builds the lookup
    match_job() uses, so ambiguity (more than one job sharing a contact)
    can be detected once instead of rescanning per message."""
    by_email: dict[str, list[tuple]] = {}
    by_phone: dict[str, list[tuple]] = {}
    for job in jobs:
        key = (job["client_name"], job.get("sheet_tab") or "", job.get("primary_job_type") or "")
        email = (job.get("customer_email") or "").strip().lower()
        phone = _normalize_phone(job.get("customer_phone") or "")
        if email:
            by_email.setdefault(email, []).append(key)
        if phone:
            by_phone.setdefault(phone, []).append(key)
    return {"by_email": by_email, "by_phone": by_phone}


def _dedupe(keys: list[tuple]) -> list[tuple]:
    return list(dict.fromkeys(keys))


def match_job(message: dict, contact_index: dict) -> dict:
    """message: {"customer_email", "customer_phone"}. Tries email first,
    then phone. More than one job shares the matched contact -> job_key is
    None (never guess) and every candidate is listed."""
    email = (message.get("customer_email") or "").strip().lower()
    if email and email in contact_index["by_email"]:
        candidates = _dedupe(contact_index["by_email"][email])
        return {"job_key": candidates[0] if len(candidates) == 1 else None,
                "candidates": candidates, "matched_by": "email"}

    phone = _normalize_phone(message.get("customer_phone") or "")
    if phone and phone in contact_index["by_phone"]:
        candidates = _dedupe(contact_index["by_phone"][phone])
        return {"job_key": candidates[0] if len(candidates) == 1 else None,
                "candidates": candidates, "matched_by": "phone"}

    return {"job_key": None, "candidates": [], "matched_by": None}


def count_ambiguous_contacts(jobs: list[dict]) -> dict:
    """How many distinct emails/phones are shared by more than one job
    (an ambiguous contact can never auto-match — see match_job())."""
    index = build_contact_index(jobs)
    ambiguous_emails = [e for e, keys in index["by_email"].items() if len(_dedupe(keys)) > 1]
    ambiguous_phones = [p for p, keys in index["by_phone"].items() if len(_dedupe(keys)) > 1]
    return {
        "ambiguous_email_count": len(ambiguous_emails),
        "ambiguous_phone_count": len(ambiguous_phones),
        "ambiguous_emails": ambiguous_emails,
        "ambiguous_phones": ambiguous_phones,
    }
