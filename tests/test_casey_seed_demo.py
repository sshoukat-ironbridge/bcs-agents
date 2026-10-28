import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from seed_casey_demo import INVENTED_FLAGS, STRONG_PRAISE, RESPONSE_WATCHES, _load_calibration_quotes  # noqa: E402


def test_invented_plus_calibration_flags_total_22():
    # 4 calibration customers each produce exactly one flag, except
    # Danielle Ewen (one flag, escalated) — see seed_calibration_customers().
    calibration_flag_count = 4
    assert len(INVENTED_FLAGS) + calibration_flag_count == 22


def test_severity_distribution_matches_spec():
    # Calibration contributes: Julie High, Danielle Critical (after
    # escalation), Caroline Medium, Betsy Critical.
    calibration_severities = ["High", "Critical", "Medium", "Critical"]
    invented_severities = [spec[1] for spec in INVENTED_FLAGS]
    counts = Counter(calibration_severities + invented_severities)
    assert counts == Counter({"Critical": 2, "High": 4, "Medium": 8, "Low": 8})


def test_strong_praise_has_8_items_all_distinct_customers():
    assert len(STRONG_PRAISE) == 8
    customers = [c for c, _ in STRONG_PRAISE]
    assert len(set(customers)) == 8


def test_response_watches_has_14_with_the_spec_status_split():
    assert len(RESPONSE_WATCHES) == 14
    statuses = Counter(w[1] for w in RESPONSE_WATCHES)
    assert statuses == Counter({"responded": 6, "waiting": 4, "overdue": 4})


def test_waiting_watches_are_due_in_the_future():
    for customer, status, received_hours_ago, due_offset_hours, _ in RESPONSE_WATCHES:
        if status == "waiting":
            # due_at = received_at + due_offset; "due in the next hours"
            # means due_at is still ahead of now, i.e. received recently.
            assert received_hours_ago < due_offset_hours


def test_overdue_watches_are_past_their_due_time():
    for customer, status, received_hours_ago, due_offset_hours, _ in RESPONSE_WATCHES:
        if status == "overdue":
            assert received_hours_ago > due_offset_hours


def test_calibration_quotes_are_loaded_verbatim_and_summary_excluded():
    quotes = _load_calibration_quotes()
    assert "Betsy Sandstrom" in quotes
    betsy_texts = [c["text"] for c in quotes["Betsy Sandstrom"]]
    assert "We'll let you know a good time to call after we receive the refund." in betsy_texts
    # The undated pre-cancellation summary must never be loaded as a
    # verbatim quote — it isn't a real message.
    assert not any("Repeated delays and failed start commitments" in t for t in betsy_texts)


def test_create_separate_customers_have_more_than_one_flag():
    counts = Counter(spec[0] for spec in INVENTED_FLAGS)
    multi_flag_customers = [c for c, n in counts.items() if n > 1]
    assert len(multi_flag_customers) >= 2
