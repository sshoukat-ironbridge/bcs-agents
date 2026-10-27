"""Webhook receivers. EXEMPT from dashboard login (see dashboard/main.py's
_PUBLIC_PREFIXES) since the caller is Dialpad, not a logged-in user.

A webhook_events row is stored ONLY when the signature verifies. For an
invalid or unsigned request, nothing is stored — only a minimal log line
(timestamp, body size, source IP) and an in-memory per-IP counter, and
the source IP is rate limited. No event processing happens here either
way — that's a later agent, reading webhook_events.
"""
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Request, Response

from config.loader import cfg
from utils.logger import get_logger

logger = get_logger("dashboard.webhooks")

router = APIRouter()

MAX_BODY_BYTES = 64 * 1024  # cap body size against abuse/resource exhaustion
DEFAULT_RATE_LIMIT_PER_MINUTE = 30

# Dialpad signs its webhook payloads as a JWT using a secret you choose
# when creating the event subscription (confirmed:
# https://developers.dialpad.com/docs/event-subscriptions.md) — the secret
# itself is never returned by Dialpad's API. The signing ALGORITHM is not
# documented; HS256 is assumed as the standard choice for a shared-secret
# JWT and is UNCONFIRMED — see docs/casey_foundation.md.
_JWT_ALGORITHM = "HS256"


class SlidingWindowRateLimiter:
    """Per-key sliding window counter — directly testable without going
    through HTTP. allow() returns False once `limit` calls have happened
    for that key within the last `window_seconds`."""

    def __init__(self, window_seconds: float = 60.0):
        self.window_seconds = window_seconds
        self._hits: dict[str, list] = defaultdict(list)

    def allow(self, key: str, limit: int, now: "float | None" = None) -> bool:
        now = now if now is not None else time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


# Module-level, shared across requests in this process. An invalid/unsigned
# attempt is never persisted, so this counter (and the rate limiter above)
# are the only record of it — in-memory is sufficient for this stage.
_rate_limiter = SlidingWindowRateLimiter()
_invalid_signature_counts: dict[str, int] = defaultdict(int)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _store_event(source: str, payload) -> None:
    from db.state_store import _db_available, _Session, WebhookEvent
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(WebhookEvent(
                source=source,
                received_at=datetime.now(timezone.utc),
                signature_valid=True,
                payload=payload,
            ))
            s.commit()
    except Exception as e:
        # Never let storage failure change the fast-ack contract below.
        logger.error(f"webhook event storage failed for {source}: {e}")


def _verify_signature(token_text: str, secret: str) -> "dict | None":
    """Returns the decoded payload if the signature verifies, else None.
    Never returns a payload for an unverified token — there is nothing to
    log or store beyond size/time/IP for those."""
    if not secret:
        return None
    try:
        return jwt.decode(token_text, secret, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


@router.post("/webhooks/dialpad/sms")
async def dialpad_sms_webhook(request: Request):
    client_ip = _client_ip(request)

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return Response(status_code=413)

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return Response(status_code=413)

    secret = cfg("dialpad_webhook_secret") or ""
    allow_unsigned = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").strip() == "1"
    if not secret and not allow_unsigned:
        logger.warning("dialpad_sms_webhook: no dialpad_webhook_secret configured, rejecting")
        return Response(status_code=401)

    rate_limit = int(cfg("casey_webhook_rate_limit_per_minute") or DEFAULT_RATE_LIMIT_PER_MINUTE)
    if not _rate_limiter.allow(client_ip, rate_limit):
        logger.warning(f"dialpad_sms_webhook: rate limit exceeded for {client_ip}")
        return Response(status_code=429)

    token_text = body.decode("utf-8", errors="replace").strip()
    payload = _verify_signature(token_text, secret) if secret else None

    if payload is not None:
        _store_event("dialpad_sms", payload)
    else:
        _invalid_signature_counts[client_ip] += 1
        logger.warning(
            f"dialpad_sms_webhook: invalid/unsigned request, "
            f"ip={client_ip}, size={len(body)} bytes, "
            f"count_from_ip={_invalid_signature_counts[client_ip]}"
        )

    return Response(status_code=200)
