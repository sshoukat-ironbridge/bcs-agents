"""Webhook receivers. EXEMPT from dashboard login (see dashboard/main.py's
_PUBLIC_PREFIXES) since the caller is Dialpad, not a logged-in user.

Each receiver: reads the raw body, verifies the signature, stores the
event in webhook_events (signature_valid recorded either way), and
returns quickly. No event processing happens here — that's a later agent,
reading webhook_events.
"""
import json
import os
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Request, Response

from config.loader import cfg
from utils.logger import get_logger

logger = get_logger("dashboard.webhooks")

router = APIRouter()

MAX_BODY_BYTES = 64 * 1024  # cap body size against abuse/resource exhaustion

# Dialpad signs its webhook payloads as a JWT using a secret you choose
# when creating the event subscription (confirmed:
# https://developers.dialpad.com/docs/event-subscriptions.md) — the secret
# itself is never returned by Dialpad's API. The signing ALGORITHM is not
# documented; HS256 is assumed as the standard choice for a shared-secret
# JWT and is UNCONFIRMED — see docs/casey_foundation.md.
_JWT_ALGORITHM = "HS256"


def _store_event(source: str, signature_valid: bool, payload) -> None:
    from db.state_store import _db_available, _Session, WebhookEvent
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(WebhookEvent(
                source=source,
                received_at=datetime.now(timezone.utc),
                signature_valid=signature_valid,
                payload=payload,
            ))
            s.commit()
    except Exception as e:
        # Never let storage failure change the fast-ack contract below.
        logger.error(f"webhook event storage failed for {source}: {e}")


@router.post("/webhooks/dialpad/sms")
async def dialpad_sms_webhook(request: Request):
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

    token_text = body.decode("utf-8", errors="replace").strip()
    signature_valid = False
    payload = None

    if secret:
        try:
            payload = jwt.decode(token_text, secret, algorithms=[_JWT_ALGORITHM])
            signature_valid = True
        except jwt.PyJWTError:
            signature_valid = False
            try:
                payload = jwt.decode(token_text, options={"verify_signature": False})
            except Exception:
                payload = {"raw": token_text}
    else:
        # ALLOW_UNSIGNED_WEBHOOKS=1, local dev only: no secret to verify
        # against, so accept plain JSON (or the raw JWT, undecoded) as-is.
        try:
            payload = json.loads(token_text)
        except Exception:
            payload = {"raw": token_text}

    _store_event("dialpad_sms", signature_valid, payload)
    return Response(status_code=200)
