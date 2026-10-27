import json
from datetime import timezone
from pathlib import Path

from agents.casey.normalizers import (
    normalize_dialpad_transcript, normalize_gmail_message, normalize_dialpad_sms,
    is_automated_email, build_contact_index, match_job, count_ambiguous_contacts,
    extract_gmail_body_text, _parse_address_list,
)

FIXTURES = Path(__file__).parent / "fixtures" / "casey"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# --- Dialpad call transcript -------------------------------------------------

def test_transcript_speaker_labels_and_moment_exclusion():
    fixture = _load("dialpad_call_transcript.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row is not None
    assert "PM: Hi Jane" in row["body_text"]
    assert "Customer: Oh hi" in row["body_text"]
    assert "Call started" not in row["body_text"]   # moment line excluded from text
    assert "Hold" not in row["body_text"]


def test_transcript_moments_kept_in_raw_ref():
    fixture = _load("dialpad_call_transcript.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert "Call started" in row["raw_ref"]
    assert "Hold" in row["raw_ref"]


def test_transcript_time_attached_as_utc():
    fixture = _load("dialpad_call_transcript.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row["occurred_at"].tzinfo == timezone.utc
    assert row["occurred_at"].hour == 14


def test_transcript_duration_is_milliseconds():
    fixture = _load("dialpad_call_transcript.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row["duration_ms"] == 184000


def test_transcript_other_fields():
    fixture = _load("dialpad_call_transcript.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row["source"] == "dialpad_call"
    assert row["external_id"] == "9988776655"
    assert row["direction"] == "outbound"
    assert row["customer_phone"] == "+15551234567"
    assert row["customer_email"] == "jane.doe@example.com"
    assert row["is_automated"] is False


def test_empty_transcript_is_skipped():
    fixture = _load("dialpad_call_empty_transcript.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row is None


# --- Gmail message ------------------------------------------------------------

def test_inbound_gmail_message():
    message = _load("gmail_message_inbound.json")
    row = normalize_gmail_message(message)
    assert row["direction"] == "inbound"
    assert row["source"] == "gmail"
    assert row["external_id"] == message["id"]
    assert row["thread_id"] == message["threadId"]
    assert row["subject"] == "Re: Deck project timeline"
    assert row["is_automated"] is False
    assert row["occurred_at"].tzinfo == timezone.utc


def test_outbound_bcs_update_is_automated():
    message = _load("gmail_message_outbound_bcs_update.json")
    row = normalize_gmail_message(message)
    assert row["direction"] == "outbound"
    assert row["is_automated"] is True


def test_bulk_list_unsubscribe_is_automated():
    message = _load("gmail_message_bulk_automated.json")
    row = normalize_gmail_message(message)
    assert row["is_automated"] is True


def test_is_automated_email_rules_directly():
    assert is_automated_email({"auto-submitted": "auto-replied"}, "hi", "a@b.com") is True
    assert is_automated_email({"auto-submitted": "no"}, "hi", "a@b.com") is False
    assert is_automated_email({"x-autoreply": "yes"}, "hi", "a@b.com") is True
    assert is_automated_email({}, "hi", "noreply@example.com") is True
    assert is_automated_email({}, "hi", "donotreply@example.com") is True
    assert is_automated_email({}, "[BCS Update] hello", "pm@example.com") is True
    assert is_automated_email({}, "hello", "pm@example.com") is False


def _b64(text):
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_extract_body_prefers_text_plain_over_html():
    payload = {"parts": [
        {"mimeType": "text/html", "body": {"data": _b64("<p>HTML version</p>")}},
        {"mimeType": "text/plain", "body": {"data": _b64("Plain version")}},
    ]}
    assert extract_gmail_body_text(payload) == "Plain version"


def test_extract_body_falls_back_to_html_stripped():
    payload = {"parts": [
        {"mimeType": "text/html", "body": {"data": _b64("<p>Hello <b>there</b></p>")}},
    ]}
    assert extract_gmail_body_text(payload) == "Hello there"


def test_extract_body_strips_quoted_reply():
    text = "Thanks for the update.\n\nOn Mon, Oct 20, 2026 at 3:00 PM Jane Doe <jane@example.com> wrote:\n> original message text"
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]}
    result = extract_gmail_body_text(payload)
    assert result == "Thanks for the update."
    assert "original message text" not in result


def test_extract_body_strips_quoted_reply_when_wrote_wraps_to_its_own_line():
    # Live-checked against a real Gmail reply: the attribution line often
    # wraps so "wrote:" lands on its own line, not at the end of "On ...".
    text = ("Thanks!\n\nLaura\n\n\nOn Tue, Oct 27, 2026 at 3:16 PM Tatiana BCS "
            "<tatiana@bluecollarscholars.net>\nwrote:\n\n> Hi Laura,\n> original quoted text")
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]}
    result = extract_gmail_body_text(payload)
    assert result == "Thanks!\n\nLaura"
    assert "original quoted text" not in result


def test_extract_body_strips_original_message_marker():
    text = "My reply here.\n-----Original Message-----\nFrom: someone"
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]}
    result = extract_gmail_body_text(payload)
    assert result == "My reply here."


def test_extract_body_strips_signature_separator():
    text = "See you then.\n-- \nJohn Smith\nSenior PM"
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]}
    assert extract_gmail_body_text(payload) == "See you then."


def test_extract_body_strips_sent_from_mobile():
    text = "Sounds good.\nSent from my iPhone"
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]}
    assert extract_gmail_body_text(payload) == "Sounds good."


def test_extract_body_caps_at_max_chars():
    text = "x" * 10000
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]}
    result = extract_gmail_body_text(payload)
    assert len(result) == 8000


def test_extract_body_nested_multipart_alternative():
    payload = {"parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("Nested plain text")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>Nested HTML</p>")}},
        ]},
    ]}
    assert extract_gmail_body_text(payload) == "Nested plain text"


def test_extract_body_empty_payload_returns_empty():
    assert extract_gmail_body_text({}) == ""


def test_normalize_gmail_message_falls_back_to_snippet_when_no_parts():
    message = _load("gmail_message_inbound.json")  # no payload.parts/body
    row = normalize_gmail_message(message)
    assert row["body_text"] == message["snippet"]


def test_parse_address_list_handles_multiple_and_display_names():
    header = 'Jane Doe <jane@example.com>, "Smith, John" <john@example.com>'
    assert _parse_address_list(header) == ["jane@example.com", "john@example.com"]


def test_parse_address_list_empty():
    assert _parse_address_list("") == []
    assert _parse_address_list(None) == []


def test_normalize_gmail_message_exposes_to_and_cc():
    message = _load("gmail_message_inbound.json")
    row = normalize_gmail_message(message)
    assert row["to_addresses"] == ["chris.s@bluecollarscholars.net"]
    assert row["cc_addresses"] == []


# --- Dialpad SMS webhook -------------------------------------------------------

def test_sms_webhook_mapping():
    payload = _load("dialpad_sms_webhook.json")
    row = normalize_dialpad_sms(payload)
    assert row["source"] == "dialpad_sms"
    assert row["external_id"] == "1234567890123456"
    assert row["direction"] == "inbound"
    assert row["customer_phone"] == "+15551234567"
    assert row["body_text"] == "Can someone call me back about the deck?"
    assert row["occurred_at"].tzinfo == timezone.utc


# --- Job matching --------------------------------------------------------------

JOBS = [
    {"client_name": "A", "sheet_tab": "to_start", "primary_job_type": "Deck",
     "customer_email": "a@example.com", "customer_phone": "555-111-2222"},
    {"client_name": "B", "sheet_tab": "to_start", "primary_job_type": "Fence",
     "customer_email": "b@example.com", "customer_phone": "555-333-4444"},
    {"client_name": "C", "sheet_tab": "in_process", "primary_job_type": "Deck",
     "customer_email": "a@example.com", "customer_phone": "555-555-6666"},  # shares A's email
]


def test_match_by_email_when_unique():
    index = build_contact_index(JOBS)
    result = match_job({"customer_email": "b@example.com"}, index)
    assert result["job_key"] == ("B", "to_start", "Fence")
    assert result["matched_by"] == "email"


def test_match_by_phone_falls_back_when_no_email_match():
    index = build_contact_index(JOBS)
    result = match_job({"customer_email": "", "customer_phone": "(555) 333-4444"}, index)
    assert result["job_key"] == ("B", "to_start", "Fence")
    assert result["matched_by"] == "phone"


def test_ambiguous_email_never_guesses():
    index = build_contact_index(JOBS)
    result = match_job({"customer_email": "a@example.com"}, index)
    assert result["job_key"] is None
    assert set(result["candidates"]) == {("A", "to_start", "Deck"), ("C", "in_process", "Deck")}
    assert result["matched_by"] == "email"


def test_no_match_returns_empty():
    index = build_contact_index(JOBS)
    result = match_job({"customer_email": "nobody@example.com", "customer_phone": "000"}, index)
    assert result == {"job_key": None, "candidates": [], "matched_by": None}


def test_match_by_phone_ignores_leading_country_code():
    jobs_with_country_code = [
        {"client_name": "D", "sheet_tab": "to_start", "primary_job_type": "Fence",
         "customer_email": "", "customer_phone": "555-777-8888"},
    ]
    index = build_contact_index(jobs_with_country_code)
    result = match_job({"customer_phone": "+1 (555) 777-8888"}, index)
    assert result["job_key"] == ("D", "to_start", "Fence")
    assert result["matched_by"] == "phone"


def test_count_ambiguous_contacts():
    result = count_ambiguous_contacts(JOBS)
    assert result["ambiguous_email_count"] == 1
    assert result["ambiguous_phone_count"] == 0
    assert "a@example.com" in result["ambiguous_emails"]
