from unittest.mock import patch

from utils.outbound import outbound_enabled


# ── outbound_enabled() itself ────────────────────────────────────────────

def test_disabled_when_oca_dry_run_set(monkeypatch):
    monkeypatch.setenv("OCA_DRY_RUN", "1")
    with patch("utils.outbound.get_config", return_value="false"):  # silent_mode off
        assert outbound_enabled() is False


def test_disabled_when_silent_mode_true(monkeypatch):
    monkeypatch.delenv("OCA_DRY_RUN", raising=False)
    with patch("utils.outbound.get_config", return_value="true"):
        assert outbound_enabled() is False


def test_enabled_when_neither_set(monkeypatch):
    monkeypatch.delenv("OCA_DRY_RUN", raising=False)
    with patch("utils.outbound.get_config", return_value="false"):
        assert outbound_enabled() is True


def test_disabled_when_both_set(monkeypatch):
    monkeypatch.setenv("OCA_DRY_RUN", "1")
    with patch("utils.outbound.get_config", return_value="true"):
        assert outbound_enabled() is False


def test_dry_run_value_other_than_1_does_not_disable(monkeypatch):
    monkeypatch.setenv("OCA_DRY_RUN", "0")
    with patch("utils.outbound.get_config", return_value="false"):
        assert outbound_enabled() is True


# ── No send function is ever called when OCA_DRY_RUN=1 ─────────────────

def test_checklist_calls_no_send_function_when_dry_run(monkeypatch, tmp_path):
    monkeypatch.setenv("OCA_DRY_RUN", "1")
    monkeypatch.chdir(tmp_path)  # so _write_dry_run_files writes under tmp_path/out
    from agents.oca.checklist import build_checklist, send_daily_checklist

    checklist = build_checklist(
        [], {"overdue": [], "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0},
        {"blank": [], "unrecognized": []},
        [],
        {"to_start": 1, "in_process": 1, "on_hold": 0, "retired_this_run": 0, "tracked_jobs": 1},
        warning_days=7,
    )

    with patch("integrations.gmail.send_email") as mock_send_email, \
         patch("integrations.slack.send_message") as mock_send_message, \
         patch("agents.oca.checklist.cfg", return_value="configured@example.com"):
        send_daily_checklist(checklist)

    mock_send_email.assert_not_called()
    mock_send_message.assert_not_called()
    assert (tmp_path / "out").is_dir()


def test_deadline_change_alert_calls_no_send_function_when_dry_run(monkeypatch):
    monkeypatch.setenv("OCA_DRY_RUN", "1")
    from agents.oca.alerts import send_deadline_change_alert

    with patch("integrations.slack.send_message") as mock_send_message:
        send_deadline_change_alert("Acme", "Gustavo", "2026-07-09", "2026-08-13")

    mock_send_message.assert_not_called()


def test_slack_send_dm_calls_no_underlying_client_when_dry_run(monkeypatch):
    monkeypatch.setenv("OCA_DRY_RUN", "1")
    from integrations.slack import send_dm

    with patch("integrations.slack._client") as mock_client:
        result = send_dm("U123", "hello")

    mock_client.assert_not_called()
    assert result is True


def test_checklist_sends_when_not_dry_run_and_silent_mode_off(monkeypatch, tmp_path):
    monkeypatch.delenv("OCA_DRY_RUN", raising=False)
    monkeypatch.chdir(tmp_path)
    from agents.oca.checklist import build_checklist, send_daily_checklist

    checklist = build_checklist(
        [], {"overdue": [], "soon": [], "no_deadline_to_start": [], "no_deadline_in_process_count": 0},
        {"blank": [], "unrecognized": []},
        [],
        {"to_start": 1, "in_process": 1, "on_hold": 0, "retired_this_run": 0, "tracked_jobs": 1},
        warning_days=7,
    )

    with patch("utils.outbound.get_config", return_value="false"), \
         patch("integrations.gmail.send_email", return_value=(True, "", "")) as mock_send_email, \
         patch("integrations.slack.send_message", return_value=True) as mock_send_message, \
         patch("agents.oca.checklist.cfg", return_value="configured@example.com"):
        send_daily_checklist(checklist)

    mock_send_email.assert_called_once()
    mock_send_message.assert_called_once()
    assert not (tmp_path / "out").exists()  # no dry-run files when it's a real (non-dry-run) send
