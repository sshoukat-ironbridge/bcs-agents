from unittest.mock import patch

from agents.casey.process_flags import process_one, run


def _analysis(**overrides):
    base = {
        "analysis_id": 1, "message_id": 100, "job_id": 10, "job_key": "A|to_start|Deck",
        "customer_name": "customer@example.com", "pm_name": "PM One", "estimator_name": "Est One",
        "severity": "Medium", "primary_reason": "Schedule delay / missed dates",
        "additional_reasons": [], "root_cause": "Undetermined (default)",
        "reason_notes": None, "issue_summary": "Delay complaint.", "sentiment": "negative",
        "occurred_at": "2026-10-20T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _config(overrides=None):
    values = {"casey_email_ana_maria": "", "casey_email_tatiana": "", "casey_email_chris": "", "casey_email_override": "",
              "casey_live_since": ""}
    values.update(overrides or {})
    return lambda key: values.get(key, "")


def _patches(existing_open_flags=None, flag_by_id=None, config_overrides=None):
    return [
        patch("db.state_store.get_open_flags_for_job", return_value=existing_open_flags or []),
        patch("db.state_store.get_config", side_effect=_config(config_overrides)),
        patch("db.state_store.find_pm", return_value=None),
        patch("db.state_store.get_flag_by_id", return_value=flag_by_id),
    ]


def test_no_open_flag_creates_a_new_one():
    flag_row = {"id": 42, "pm_name": "PM One", "severity": "Medium", "job_key": "A|to_start|Deck",
                "customer_name": "c", "primary_reason": "Schedule delay / missed dates"}
    patches = _patches(flag_by_id=flag_row)
    with patch("db.state_store.create_flag_row", return_value=42) as mock_create, \
         patch("db.state_store.create_flag_event") as mock_event, \
         patch("db.state_store.create_notification") as mock_notify:
        for p in patches:
            p.start()
        try:
            result = process_one(_analysis(), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    assert result["action"] == "create"
    assert result["flag_id"] == 42
    mock_create.assert_called_once()
    assert mock_create.call_args[0][0]["dedup_key"] == "A|to_start|Deck:Schedule delay / missed dates"
    mock_event.assert_called_once()
    assert mock_event.call_args[0][0]["event_type"] == "created"
    assert mock_event.call_args[0][0]["actor"] == "casey"
    mock_notify.assert_called()  # suppressed (backfill, casey_live_since unset) but still stored


def test_same_complaint_higher_severity_escalates():
    existing = [{"id": 5, "job_key": "A|to_start|Deck", "primary_reason": "Schedule delay / missed dates", "severity": "Low"}]
    patches = _patches(existing_open_flags=existing, flag_by_id={"id": 5, "pm_name": "PM One", "severity": "Medium",
                                                                  "job_key": "A|to_start|Deck", "customer_name": "c",
                                                                  "primary_reason": "Schedule delay / missed dates"})
    with patch("db.state_store.update_flag_row") as mock_update, \
         patch("db.state_store.create_flag_event") as mock_event, \
         patch("db.state_store.create_notification"):
        for p in patches:
            p.start()
        try:
            result = process_one(_analysis(severity="Medium"), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    assert result["action"] == "escalate"
    assert result["flag_id"] == 5
    mock_update.assert_called_once_with(5, severity="Medium", latest_message_id=100)
    assert mock_event.call_args[0][0]["event_type"] == "severity_escalated"
    assert mock_event.call_args[0][0]["old_value"] == "Low"
    assert mock_event.call_args[0][0]["new_value"] == "Medium"


def test_improving_sentiment_is_an_event_never_a_close():
    existing = [{"id": 5, "job_key": "A|to_start|Deck", "primary_reason": "Schedule delay / missed dates", "severity": "High"}]
    patches = _patches(existing_open_flags=existing)
    with patch("db.state_store.create_flag_event") as mock_event, \
         patch("db.state_store.update_flag_row") as mock_update, \
         patch("db.state_store.create_notification") as mock_notify:
        for p in patches:
            p.start()
        try:
            result = process_one(_analysis(severity="High", sentiment="positive"), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    assert result["action"] == "note_improvement"
    assert mock_event.call_args[0][0]["event_type"] == "improving_sentiment"
    mock_update.assert_not_called()  # never lowers or closes
    mock_notify.assert_not_called()  # no notification for an improvement


def test_different_complaint_creates_separate_flag():
    existing = [{"id": 5, "job_key": "A|to_start|Deck", "primary_reason": "Schedule delay / missed dates", "severity": "Low"}]
    patches = _patches(existing_open_flags=existing, flag_by_id={"id": 99, "pm_name": "PM One", "severity": "Medium",
                                                                  "job_key": "A|to_start|Deck", "customer_name": "c",
                                                                  "primary_reason": "Workmanship / quality of work"})
    with patch("db.state_store.create_flag_row", return_value=99) as mock_create, \
         patch("db.state_store.create_flag_event"), \
         patch("db.state_store.create_notification"):
        for p in patches:
            p.start()
        try:
            result = process_one(_analysis(primary_reason="Workmanship / quality of work"), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    assert result["action"] == "create_separate"
    assert result["flag_id"] == 99
    mock_create.assert_called_once()


def test_no_op_when_nothing_changes():
    existing = [{"id": 5, "job_key": "A|to_start|Deck", "primary_reason": "Schedule delay / missed dates", "severity": "Medium"}]
    patches = _patches(existing_open_flags=existing)
    with patch("db.state_store.create_flag_event") as mock_event:
        for p in patches:
            p.start()
        try:
            result = process_one(_analysis(severity="Medium"), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    assert result["action"] == "no_op"
    mock_event.assert_called_once()  # still marks the analysis processed
    assert mock_event.call_args[0][0]["event_type"] == "note"


def test_dry_run_never_writes_anything():
    patches = _patches()
    with patch("db.state_store.create_flag_row") as mock_create_flag, \
         patch("db.state_store.create_flag_event") as mock_event, \
         patch("db.state_store.create_notification") as mock_notify:
        for p in patches:
            p.start()
        try:
            result = process_one(_analysis(), dry_run=True)
        finally:
            for p in patches:
                p.stop()
    assert result["action"] == "create"
    mock_create_flag.assert_not_called()
    mock_event.assert_not_called()
    mock_notify.assert_not_called()


def test_notifications_suppressed_when_live_since_unset():
    flag_row = {"id": 42, "pm_name": "PM One", "severity": "Medium", "job_key": "A|to_start|Deck",
                "customer_name": "c", "primary_reason": "Schedule delay / missed dates"}
    patches = _patches(config_overrides={"casey_live_since": ""}, flag_by_id=flag_row)
    with patch("db.state_store.create_flag_row", return_value=42), \
         patch("db.state_store.create_flag_event"), \
         patch("db.state_store.create_notification") as mock_notify:
        for p in patches:
            p.start()
        try:
            process_one(_analysis(), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    for call in mock_notify.call_args_list:
        row = call.args[0]
        assert row["status"] == "suppressed"
        assert row["reason"] == "backfill"


def test_escalation_notification_says_so_explicitly():
    existing = [{"id": 5, "job_key": "A|to_start|Deck", "primary_reason": "Schedule delay / missed dates", "severity": "Low"}]
    patches = _patches(existing_open_flags=existing, flag_by_id={"id": 5, "pm_name": "PM One", "severity": "Medium",
                                                                  "job_key": "A|to_start|Deck", "customer_name": "c",
                                                                  "primary_reason": "Schedule delay / missed dates"})
    with patch("db.state_store.update_flag_row"), \
         patch("db.state_store.create_flag_event"), \
         patch("db.state_store.create_notification") as mock_notify:
        for p in patches:
            p.start()
        try:
            process_one(_analysis(severity="Medium"), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    for call in mock_notify.call_args_list:
        assert "ESCALATED to MEDIUM from LOW" in call.args[0]["message"]


def test_notification_includes_dashboard_link_when_configured():
    flag_row = {"id": 42, "pm_name": "PM One", "severity": "Medium", "job_key": "A|to_start|Deck",
                "customer_name": "c", "primary_reason": "Schedule delay / missed dates"}
    patches = _patches(config_overrides={"casey_dashboard_base_url": "https://dash.example"}, flag_by_id=flag_row)
    with patch("db.state_store.create_flag_row", return_value=42), \
         patch("db.state_store.create_flag_event"), \
         patch("db.state_store.create_notification") as mock_notify:
        for p in patches:
            p.start()
        try:
            process_one(_analysis(), dry_run=False)
        finally:
            for p in patches:
                p.stop()
    assert any("https://dash.example/casey/flags/42" in call.args[0]["message"] for call in mock_notify.call_args_list)


def test_run_tallies_across_multiple_analyses():
    analyses = [_analysis(message_id=1, job_key="A|to_start|Deck"), _analysis(message_id=2, job_key="B|to_start|Fence")]
    with patch("db.state_store.get_unprocessed_analyses", return_value=analyses), \
         patch("db.state_store.get_open_flags_for_job", return_value=[]), \
         patch("db.state_store.get_config", side_effect=_config()), \
         patch("db.state_store.find_pm", return_value=None), \
         patch("db.state_store.get_flag_by_id", return_value={"id": 1, "pm_name": "PM One", "severity": "Medium",
                                                                   "job_key": "A|to_start|Deck", "customer_name": "c",
                                                                   "primary_reason": "Schedule delay / missed dates"}), \
         patch("db.state_store.create_flag_row", return_value=1), \
         patch("db.state_store.create_flag_event"), \
         patch("db.state_store.create_notification"):
        stats = run(dry_run=False)
    assert stats["processed"] == 2
    assert stats["create"] == 2
