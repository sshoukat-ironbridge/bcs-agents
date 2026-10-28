import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from casey_review_pack import (  # noqa: E402
    truncate, strip_dashes, clean, probably_no_reply_needed, why_flagged_sentence,
    review_hint, age_bucket, reconcile, RANDOM_SAMPLE_SEED,
)


# --- truncate ------------------------------------------------------------------

def test_truncate_short_text_unchanged():
    assert truncate("hello", 220) == "hello"


def test_truncate_long_text_cut_with_ellipsis():
    text = "x" * 300
    result = truncate(text, 220)
    assert len(result) == 223  # 220 chars + "..."
    assert result.endswith("...")


def test_truncate_handles_none():
    assert truncate(None) == ""


# --- strip_dashes / clean --------------------------------------------------------

def test_strip_dashes_removes_em_and_en_dash():
    text = "failed partnership — BCS and the architect – unclear"
    result = strip_dashes(text)
    assert "—" not in result
    assert "–" not in result


def test_clean_escapes_html_and_strips_dashes():
    result = clean("5 < 10 & she said — ok")
    assert "<" not in result
    assert "&" not in result or "&amp;" in result
    assert "—" not in result


def test_clean_handles_none():
    assert clean(None) == ""


# --- probably_no_reply_needed ---------------------------------------------------

def test_short_message_probably_no_reply():
    assert probably_no_reply_needed("Thanks, that works for us.") is True


def test_question_mark_always_needs_reply():
    assert probably_no_reply_needed("Will the crew be here Monday?") is False


def test_long_substantive_message_needs_reply():
    text = "We are still waiting on the materials and nobody has given us any update this week at all."
    assert probably_no_reply_needed(text) is False


def test_ack_phrase_probably_no_reply():
    assert probably_no_reply_needed("Sounds good, appreciate it") is True


def test_empty_text_counts_as_short():
    assert probably_no_reply_needed("") is True


# --- why_flagged_sentence --------------------------------------------------------

def test_why_flagged_sentence_with_no_prior_contact():
    due = datetime(2026, 10, 26, 9, 20, tzinfo=timezone.utc)
    sentence = why_flagged_sentence(due, None, None)
    assert "Last outgoing email to this customer: none." in sentence
    assert "Last connected call: none." in sentence
    assert "—" not in sentence and "–" not in sentence


def test_why_flagged_sentence_with_prior_contact():
    due = datetime(2026, 10, 26, 9, 20, tzinfo=timezone.utc)
    last_email = datetime(2026, 10, 20, 19, 10, tzinfo=timezone.utc)
    sentence = why_flagged_sentence(due, last_email, None)
    assert "Last outgoing email to this customer: Oct" in sentence
    assert "Last connected call: none." in sentence


# --- review_hint -----------------------------------------------------------------

def test_review_hint_ambiguous_contact():
    assert review_hint("ambiguous", False, "a long real question about something?") == "Contact shared by several jobs"


def test_review_hint_cross_pm():
    assert review_hint("matched", True, "a long real question about something?") == "Mailbox owner is not the job PM"


def test_review_hint_no_text():
    assert review_hint("matched", False, "") == "No hint"
    assert review_hint("matched", False, None) == "No hint"


def test_review_hint_short_message():
    assert review_hint("matched", False, "Thanks so much!") == "Short message, may need no reply"


def test_review_hint_needs_reply():
    text = "We still have not heard back about the schedule and it has been over a week now, can you help?"
    assert review_hint("matched", False, text) == "Looks like it needs a reply"


# --- age_bucket --------------------------------------------------------------

def test_age_bucket_boundaries():
    assert age_bucket(1) == "Under 24h late"
    assert age_bucket(23.9) == "Under 24h late"
    assert age_bucket(24) == "1 to 3 days"
    assert age_bucket(72) == "1 to 3 days"
    assert age_bucket(72.1) == "Over 3 days"
    assert age_bucket(200) == "Over 3 days"


# --- reconcile -----------------------------------------------------------------

def test_reconcile_detects_mismatch():
    counts = {"flags_by_severity": {"High": 2, "Low": 1}, "watches_by_status": {"waiting": 1, "overdue": 2}}
    flag_rows = [1, 2]  # only 2, but counts say 3
    watch_data = {"waiting_overdue": [1, 2, 3]}
    checks = reconcile(counts, flag_rows, watch_data)
    flags_check = next(c for c in checks if c[0] == "Flags (total)")
    assert flags_check[3] is False  # 3 in DB, 2 in "PDF"


def test_reconcile_all_ok_when_counts_match():
    counts = {"flags_by_severity": {"High": 2}, "watches_by_status": {"waiting": 1, "overdue": 1}}
    flag_rows = [1, 2]
    watch_data = {"waiting_overdue": [1, 2]}
    checks = reconcile(counts, flag_rows, watch_data)
    assert all(c[3] for c in checks)


def test_random_sample_seed_is_fixed_and_documented():
    # The seed used for the reproducible random sample must be a fixed,
    # stated constant - not re-derived from the clock or anything else.
    assert isinstance(RANDOM_SAMPLE_SEED, int)
