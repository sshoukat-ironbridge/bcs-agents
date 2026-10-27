from datetime import datetime, timedelta, timezone

from agents.casey.response_watch import classify_response, due_at, DEFAULT_WINDOW_HOURS

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def _inbound(**overrides):
    inbound = {
        "received_at": T0,
        "thread_id": "thread-1",
        "customer_phone": "+15551234567",
        "is_automated": False,
    }
    inbound.update(overrides)
    return inbound


def test_automated_inbound_is_excluded():
    inbound = _inbound(is_automated=True)
    assert classify_response(inbound, [], T0 + timedelta(hours=1)) == "excluded"


def test_no_response_within_window_is_waiting():
    inbound = _inbound()
    now = T0 + timedelta(hours=1)
    assert classify_response(inbound, [], now) == "waiting"


def test_no_response_after_window_is_overdue():
    inbound = _inbound()
    now = T0 + timedelta(hours=25)
    assert classify_response(inbound, [], now) == "overdue"


def test_exactly_at_window_boundary_is_not_yet_overdue():
    inbound = _inbound()
    now = due_at(T0, DEFAULT_WINDOW_HOURS)
    assert classify_response(inbound, [], now) == "waiting"


def test_outbound_email_same_thread_counts_as_responded():
    inbound = _inbound()
    reply = {"source": "gmail", "direction": "outbound", "thread_id": "thread-1",
              "occurred_at": T0 + timedelta(hours=2)}
    assert classify_response(inbound, [reply], T0 + timedelta(hours=3)) == "responded"


def test_outbound_email_different_thread_does_not_count():
    inbound = _inbound()
    reply = {"source": "gmail", "direction": "outbound", "thread_id": "thread-2",
              "occurred_at": T0 + timedelta(hours=2)}
    assert classify_response(inbound, [reply], T0 + timedelta(hours=25)) == "overdue"


def test_connected_outbound_call_counts_as_responded():
    inbound = _inbound()
    call = {"source": "dialpad_call", "direction": "outbound", "customer_phone": "+15551234567",
            "occurred_at": T0 + timedelta(hours=2), "duration_ms": 45000}
    assert classify_response(inbound, [call], T0 + timedelta(hours=3)) == "responded"


def test_missed_call_zero_duration_does_not_count():
    inbound = _inbound()
    call = {"source": "dialpad_call", "direction": "outbound", "customer_phone": "+15551234567",
            "occurred_at": T0 + timedelta(hours=2), "duration_ms": 0}
    assert classify_response(inbound, [call], T0 + timedelta(hours=25)) == "overdue"


def test_inbound_call_does_not_count_as_a_response():
    inbound = _inbound()
    call = {"source": "dialpad_call", "direction": "inbound", "customer_phone": "+15551234567",
            "occurred_at": T0 + timedelta(hours=2), "duration_ms": 45000}
    assert classify_response(inbound, [call], T0 + timedelta(hours=25)) == "overdue"


def test_sms_does_not_count_by_default():
    inbound = _inbound()
    sms = {"source": "dialpad_sms", "direction": "outbound", "customer_phone": "+15551234567",
           "occurred_at": T0 + timedelta(hours=2)}
    assert classify_response(inbound, [sms], T0 + timedelta(hours=25)) == "overdue"


def test_sms_counts_when_flag_enabled():
    inbound = _inbound()
    sms = {"source": "dialpad_sms", "direction": "outbound", "customer_phone": "+15551234567",
           "occurred_at": T0 + timedelta(hours=2)}
    result = classify_response(inbound, [sms], T0 + timedelta(hours=3), sms_counts_as_response=True)
    assert result == "responded"


def test_response_before_received_at_does_not_count():
    inbound = _inbound()
    reply = {"source": "gmail", "direction": "outbound", "thread_id": "thread-1",
              "occurred_at": T0 - timedelta(hours=1)}
    assert classify_response(inbound, [reply], T0 + timedelta(hours=25)) == "overdue"


def test_response_after_window_does_not_count():
    inbound = _inbound()
    reply = {"source": "gmail", "direction": "outbound", "thread_id": "thread-1",
              "occurred_at": T0 + timedelta(hours=25)}
    assert classify_response(inbound, [reply], T0 + timedelta(hours=26)) == "overdue"


def test_custom_window_hours():
    inbound = _inbound()
    reply = {"source": "gmail", "direction": "outbound", "thread_id": "thread-1",
              "occurred_at": T0 + timedelta(hours=5)}
    # 4-hour window: the reply at hour 5 is too late.
    assert classify_response(inbound, [reply], T0 + timedelta(hours=6), window_hours=4) == "overdue"
