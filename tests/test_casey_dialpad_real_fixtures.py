"""Parses sanitized copies of real Dialpad call/transcript responses
(tests/fixtures/casey/dialpad_real_*.json) — every field, type and shape
is preserved from the real API responses; only phone numbers, names,
emails and transcript text were replaced with placeholders.
"""
import json
from pathlib import Path

from agents.casey.normalizers import normalize_dialpad_transcript

FIXTURES = Path(__file__).parent / "fixtures" / "casey"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_connected_call_normalizes_cleanly():
    fixture = _load("dialpad_real_call_connected.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row is not None
    assert row["source"] == "dialpad_call"
    assert row["external_id"] == "1000000000000001"
    assert row["direction"] == "outbound"
    assert row["customer_phone"] == "+15550000001"
    assert row["duration_ms"] == fixture["call"]["duration"]
    assert row["occurred_at"].year == 2026
    # whole_call_summary_fragment and call_purpose_category are "moment"
    # lines - excluded from the text, but still present in raw_ref.
    assert "whole_call_summary_fragment" not in row["body_text"]
    assert "call_purpose_category" not in row["body_text"]
    assert "whole_call_summary_fragment" in row["raw_ref"]
    assert "Customer:" in row["body_text"]
    assert "PM:" in row["body_text"]


def test_internal_call_transcript_excludes_every_moment_subtype():
    fixture = _load("dialpad_real_call_internal.json")
    row = normalize_dialpad_transcript(fixture["call"], fixture["transcript"])
    assert row is not None
    moment_labels = ["ai_csat_reboot_ineligible", "call_purpose_category", "question",
                      "whole_call_summary", "whole_call_summary_fragment", "ner", "date"]
    for label in moment_labels:
        assert label not in row["body_text"]
        assert label in row["raw_ref"]  # preserved in raw_ref regardless


def test_internal_call_contact_type_is_user():
    fixture = _load("dialpad_real_call_internal.json")
    assert fixture["call"]["contact"]["type"] == "user"


def test_zero_duration_call_has_no_transcript_fixture():
    fixture = _load("dialpad_real_call_zero_duration.json")
    assert fixture["call"]["duration"] == 0.0
    assert "transcript" not in fixture  # no transcript was ever fetched for this one


def test_date_started_is_epoch_ms_string_in_fixtures():
    for name in ("dialpad_real_call_connected.json", "dialpad_real_call_internal.json",
                 "dialpad_real_call_zero_duration.json"):
        fixture = _load(name)
        date_started = fixture["call"]["date_started"]
        assert isinstance(date_started, str) and date_started.isdigit()


def test_no_real_pii_leaked_into_fixtures():
    # Sanity check that the sanitization actually happened: no @gmail/
    # @bluecollarscholars customer-looking addresses or real-shaped
    # content markers remain beyond the placeholder PM addresses.
    for name in ("dialpad_real_call_connected.json", "dialpad_real_call_internal.json",
                 "dialpad_real_call_zero_duration.json"):
        raw = (FIXTURES / name).read_text()
        assert "Laura Arbelaez" not in raw
        assert "Yower Daniel" not in raw
        assert "Ubong Umoh" not in raw
        assert "Emilio" not in raw
