import inspect
from datetime import datetime, timezone

from dashboard.casey_queries import (
    _role_filter, _week_buckets, _rate_or_small_sample, _aging_bucket,
    MIN_SAMPLE_FOR_RATE, SMALL_SAMPLE_LABEL, AGING_BUCKET_LABELS,
)


# --- _role_filter: reuses agents/casey/access.py's role->field mapping -------

def test_full_visibility_role_sees_everything():
    assert _role_filter({"role": "admin", "linked_name": ""}) == ("full", None)
    assert _role_filter({"role": "leadership", "linked_name": ""}) == ("full", None)
    assert _role_filter({"role": "cx_coordinator", "linked_name": ""}) == ("full", None)


def test_pm_role_is_restricted_to_their_field():
    assert _role_filter({"role": "pm", "linked_name": "Gustavo"}) == ("field", "pm_name")


def test_estimator_role_is_restricted_to_their_field():
    assert _role_filter({"role": "estimator", "linked_name": "Sam"}) == ("field", "estimator_name")


def test_restricted_role_without_linked_name_sees_nothing():
    assert _role_filter({"role": "pm", "linked_name": ""}) == ("none", None)


def test_unknown_role_sees_nothing():
    assert _role_filter({"role": "superuser", "linked_name": "whoever"}) == ("none", None)


def test_missing_viewer_sees_nothing():
    assert _role_filter(None) == ("none", None)


# --- _week_buckets -------------------------------------------------------------

def test_week_buckets_returns_requested_count_oldest_first():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)  # a Wednesday
    buckets = _week_buckets(8, now=now)
    assert len(buckets) == 8
    starts = [b[0] for b in buckets]
    assert starts == sorted(starts)
    # Last bucket contains `now`.
    last_start, last_end, _ = buckets[-1]
    assert last_start <= now < last_end


def test_week_buckets_are_monday_to_monday():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    buckets = _week_buckets(1, now=now)
    start, end, _ = buckets[0]
    assert start.weekday() == 0  # Monday
    assert (end - start).days == 7


# --- _rate_or_small_sample / _aging_bucket -------------------------------------

def test_rate_or_small_sample_below_minimum_is_the_label():
    assert _rate_or_small_sample(10, MIN_SAMPLE_FOR_RATE - 1) == SMALL_SAMPLE_LABEL


def test_rate_or_small_sample_at_minimum_computes_a_rate():
    result = _rate_or_small_sample(10, MIN_SAMPLE_FOR_RATE)
    assert result == 2.0


def test_aging_bucket_boundaries():
    assert _aging_bucket(0) == "0-3"
    assert _aging_bucket(3) == "0-3"
    assert _aging_bucket(4) == "4-7"
    assert _aging_bucket(7) == "4-7"
    assert _aging_bucket(8) == "8-14"
    assert _aging_bucket(14) == "8-14"
    assert _aging_bucket(15) == "15+"
    assert _aging_bucket(100) == "15+"


def test_aging_bucket_labels_cover_every_bucket():
    assert AGING_BUCKET_LABELS == ["0-3", "4-7", "8-14", "15+"]


# --- Demo-mode isolation: regression/documentation tests ----------------------
# Every query function here, and every production agent-pipeline query it
# must never collide with, filters explicitly on is_demo — confirmed by
# inspecting the source (the same pattern this codebase already uses for
# DB-touching code with no isolated test database to run a live query
# against; see tests/test_casey_run_watches.py's precedent).

def test_dashboard_queries_filter_by_is_demo():
    import dashboard.casey_queries as q

    for fn in (q.kpis, q.flags_by_severity, q.flags_by_category, q.flags_by_status,
               q.aging_buckets, q.flags_opened_closed_by_week, q.sentiment_by_week,
               q.flags_list, q.flag_detail, q.filter_options,
               q.people_workload, q.strong_praise_list, q.praise_weekly_counts, q.praise_by_pm,
               q.response_watches_table, q.response_rate_by_pm, q.flags_by_category_weekly,
               q.sentiment_trajectory_by_pm, q.worsening_projects):
        source = inspect.getsource(fn)
        assert "is_demo" in source, f"{fn.__name__} does not filter by is_demo"


def test_real_pipeline_queries_never_touch_demo_rows():
    import db.state_store as state_store_module

    for fn_name in ("get_messages_to_analyze", "get_message_history", "get_open_flag_for_job",
                     "get_open_flags_for_job", "get_unprocessed_analyses", "get_open_response_watches"):
        source = inspect.getsource(getattr(state_store_module, fn_name))
        assert "is_demo" in source, f"{fn_name} does not exclude demo rows"


def test_casey_demo_mode_defaults_true_and_respects_explicit_false():
    from unittest.mock import patch
    from db.state_store import casey_demo_mode

    with patch("db.state_store.get_config", return_value=None):
        assert casey_demo_mode() is True
    with patch("db.state_store.get_config", return_value="true"):
        assert casey_demo_mode() is True
    with patch("db.state_store.get_config", return_value="false"):
        assert casey_demo_mode() is False
