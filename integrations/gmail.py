import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import base64
from email.mime.text import MIMEText
from utils.logger import get_logger

logger = get_logger("integrations.gmail")


def _get_gmail_service(sender_email: str):
    """Build Gmail API service impersonating sender_email via domain-wide delegation."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_value = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_value:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    if sa_value.startswith("{"):
        sa_info = json.loads(sa_value)
    else:
        with open(sa_value) as f:
            sa_info = json.load(f)

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://mail.google.com/"],
        subject=sender_email,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(
    sender_email: str,
    to_email: str,
    subject: str,
    body_html: str,
    cc_email: str = "",
    thread_id: str = "",
    message_id: str = "",
    retry: bool = True,
) -> "tuple[bool, str, str]":
    """Send an email via Gmail API with domain-wide delegation. Retries once on failure.

    thread_id / message_id: when provided, the outgoing message is sent as a
    reply in the existing Gmail thread (In-Reply-To + References headers + threadId
    body param) so all Casey emails to one customer stay in a single thread.

    Returns (success, returned_gmail_thread_id, rfc_message_id).
    rfc_message_id is the RFC 2822 Message-ID header fetched after the send —
    pass it as message_id next time to maintain the reply chain.
    """
    from db.state_store import get_config
    if get_config("silent_mode") == "true":
        logger.info(f"SILENT MODE — suppressed email to {to_email}")
        return False, "", ""

    import time as _time

    def _attempt() -> "tuple[bool, str, str]":
        service = _get_gmail_service(sender_email)
        message = MIMEText(body_html, "html")
        message["to"] = to_email
        message["from"] = sender_email
        message["subject"] = subject
        if cc_email:
            message["cc"] = cc_email
        if message_id:
            message["In-Reply-To"] = message_id
            message["References"] = message_id
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body: dict = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        result = service.users().messages().send(userId="me", body=body).execute()
        returned_thread_id = result.get("threadId", "")
        gmail_msg_id = result.get("id", "")
        # Fetch the sent message to get its RFC 2822 Message-ID for future In-Reply-To headers
        rfc_message_id = ""
        if gmail_msg_id:
            try:
                sent_msg = service.users().messages().get(
                    userId="me",
                    id=gmail_msg_id,
                    format="metadata",
                    metadataHeaders=["Message-ID"],
                ).execute()
                headers = sent_msg.get("payload", {}).get("headers", [])
                rfc_message_id = next(
                    (h["value"] for h in headers if h.get("name") == "Message-ID"), ""
                )
            except Exception as e_fetch:
                logger.warning(f"Could not fetch Message-ID header after send: {e_fetch}")
        logger.info(
            f"Email sent from {sender_email} to {to_email} (cc={cc_email}) "
            f"id={gmail_msg_id} thread={returned_thread_id}"
        )
        return True, returned_thread_id, rfc_message_id

    try:
        return _attempt()
    except Exception as e:
        logger.warning(f"Gmail first attempt failed ({sender_email} → {to_email}): {e}")
        if retry:
            _time.sleep(30)
            try:
                return _attempt()
            except Exception as e2:
                logger.error(f"Gmail retry failed ({sender_email} → {to_email}): {e2}")
        return False, "", ""


def send_email_with_attachment(
    sender_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    attachment_bytes: bytes,
    attachment_filename: str,
    attachment_mime_type: str = "application/pdf",
) -> bool:
    """Send an email with a binary attachment via Gmail API with domain-wide delegation."""
    from db.state_store import get_config
    if get_config("silent_mode") == "true":
        logger.info(f"SILENT MODE — suppressed email with attachment to {to_email}: {subject}")
        return False

    import time as _time
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders as _encoders

    def _attempt() -> bool:
        service = _get_gmail_service(sender_email)
        msg = MIMEMultipart()
        msg["to"] = to_email
        msg["from"] = sender_email
        msg["subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        mime_main, mime_sub = attachment_mime_type.split("/", 1)
        part = MIMEBase(mime_main, mime_sub)
        part.set_payload(attachment_bytes)
        _encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=attachment_filename)
        msg.attach(part)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(
            f"Email with attachment sent from {sender_email} to {to_email} "
            f"file={attachment_filename} id={result.get('id')}"
        )
        return True

    try:
        return _attempt()
    except Exception as e:
        logger.warning(f"Gmail attachment send failed ({sender_email} → {to_email}): {e}")
        try:
            _time.sleep(30)
            return _attempt()
        except Exception as e2:
            logger.error(f"Gmail attachment send retry failed ({sender_email} → {to_email}): {e2}")
            return False


