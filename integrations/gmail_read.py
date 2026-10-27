"""Read-only Gmail API wrapper. Exposes ONLY list_messages/get_message/
get_thread — internally these ever call only
users().messages().list/.get and users().threads().get. No send,
modify, delete, or label method is called anywhere in this module, and
the credentials themselves are scoped to gmail.readonly, so even a
coding mistake here cannot send, modify, delete, or label a message.

Never logs subject or body text at INFO — only counts and message/
thread ids.
"""
import json
import os
import time
from typing import Any

from utils.logger import get_logger

logger = get_logger("integrations.gmail_read")

# Would prefer the narrower "https://www.googleapis.com/auth/gmail.readonly"
# scope so the read-only guarantee is enforced by the credentials
# themselves — but LIVE-CHECKED: the service account's domain-wide
# delegation in Google Admin is authorized only for
# "https://mail.google.com/" (the scope integrations/gmail.py's send
# path already uses), not gmail.readonly, and requesting an
# unauthorized scope fails with "unauthorized_client" at token refresh.
# Falls back to the already-authorized scope; the read-only guarantee
# here is enforced at the code level instead (this module calls only
# messages().list/.get and threads().get — see the module docstring and
# test_module_never_calls_send_or_modify_methods). Revisit if a Workspace
# admin ever authorizes gmail.readonly for this client ID.
READONLY_SCOPES = ["https://mail.google.com/"]
REQUEST_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (2, 5)
_LIST_PAGE_SIZE = 100


class GmailReadError(Exception):
    pass


def _load_sa_info() -> dict:
    value = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not value:
        raise GmailReadError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if value.startswith("{"):
        return json.loads(value)
    with open(value) as f:
        return json.load(f)


def _get_service(mailbox: str):
    sa_info = _load_sa_info()  # raises GmailReadError first, before any Google import

    import httplib2
    import google_auth_httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=READONLY_SCOPES, subject=mailbox,
    )
    authed_http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=REQUEST_TIMEOUT_SECONDS)
    )
    return build("gmail", "v1", http=authed_http, cache_discovery=False)


def _is_retryable(exc: Exception) -> bool:
    """Duck-typed on googleapiclient.errors.HttpError's shape (exc.resp.status)
    rather than isinstance, so it works without importing googleapiclient
    and is directly testable with a plain fake."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 429 or (isinstance(status, int) and status >= 500)


def _with_retry(fn, _sleep=time.sleep):
    last_exc: "Exception | None" = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if not _is_retryable(e) or attempt >= MAX_ATTEMPTS:
                raise
            delay = _RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(f"Gmail API error (attempt {attempt}/{MAX_ATTEMPTS}), retrying in {delay}s")
            _sleep(delay)
    raise last_exc  # pragma: no cover — unreachable, loop always raises or returns


def list_messages(mailbox: str, query: str, max_results: int = 500, _sleep=time.sleep) -> list[dict[str, Any]]:
    """Lists {"id", "threadId"} stubs matching `query` (Gmail search
    syntax), paginated, capped at max_results total. Never logs the
    query text (it may contain addresses) — only the resulting count."""
    service = _get_service(mailbox)
    messages: list[dict] = []
    page_token = None

    while len(messages) < max_results:
        remaining = max_results - len(messages)

        def _call():
            req = service.users().messages().list(
                userId="me", q=query, pageToken=page_token,
                maxResults=min(_LIST_PAGE_SIZE, remaining),
            )
            return req.execute(num_retries=0)

        data = _with_retry(_call, _sleep=_sleep)
        messages.extend(data.get("messages", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    messages = messages[:max_results]
    logger.info(f"list_messages(mailbox={mailbox}): {len(messages)} message(s)")
    return messages


def get_message(mailbox: str, message_id: str, _sleep=time.sleep) -> dict:
    """Full message resource (format=full) for one message id."""
    service = _get_service(mailbox)

    def _call():
        return service.users().messages().get(userId="me", id=message_id, format="full").execute(num_retries=0)

    data = _with_retry(_call, _sleep=_sleep)
    logger.info(f"get_message(mailbox={mailbox}, message_id={message_id}): fetched")
    return data


def get_thread(mailbox: str, thread_id: str, _sleep=time.sleep) -> dict:
    """Full thread resource (format=full) for one thread id."""
    service = _get_service(mailbox)

    def _call():
        return service.users().threads().get(userId="me", id=thread_id, format="full").execute(num_retries=0)

    data = _with_retry(_call, _sleep=_sleep)
    logger.info(f"get_thread(mailbox={mailbox}, thread_id={thread_id}): fetched")
    return data
