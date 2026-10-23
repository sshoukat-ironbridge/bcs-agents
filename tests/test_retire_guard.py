from agents.oca.checks import should_skip_retire as _should_skip_retire, MAX_RETIRE_COUNT, MAX_RETIRE_FRACTION


def test_small_count_well_under_fraction_is_not_skipped():
    # 3 of 100 — under both the absolute count and the 25% fraction.
    assert _should_skip_retire(3, 100) is False


def test_count_over_absolute_threshold_is_skipped():
    assert _should_skip_retire(MAX_RETIRE_COUNT + 1, 1000) is True


def test_count_at_absolute_threshold_is_not_skipped():
    # "more than 15" — exactly 15 should NOT trigger the guard.
    assert _should_skip_retire(MAX_RETIRE_COUNT, 1000) is False


def test_fraction_over_threshold_is_skipped_even_under_absolute_count():
    # 10 of 20 = 50%, well over MAX_RETIRE_FRACTION, but 10 is under
    # MAX_RETIRE_COUNT (15).
    assert MAX_RETIRE_FRACTION < 0.5
    assert _should_skip_retire(10, 20) is True


def test_fraction_at_exactly_threshold_is_not_skipped():
    # Exactly MAX_RETIRE_FRACTION of the total — "more than" should not trigger.
    total = 20
    candidates_at_threshold = int(total * MAX_RETIRE_FRACTION)
    assert _should_skip_retire(candidates_at_threshold, total) is False


def test_zero_active_jobs_does_not_divide_by_zero():
    assert _should_skip_retire(0, 0) is False


def test_total_active_zero_falls_back_to_absolute_count_only():
    # Can't compute a fraction of zero — must not raise ZeroDivisionError,
    # and falls back to the absolute-count check alone.
    assert _should_skip_retire(5, 0) is False
    assert _should_skip_retire(MAX_RETIRE_COUNT + 1, 0) is True
