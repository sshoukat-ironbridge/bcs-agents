import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from casey_send_test import build_synthetic_flag, plan_test_notifications, preview  # noqa: E402


def test_synthetic_flag_is_critical_and_never_a_real_id():
    flag = build_synthetic_flag()
    assert flag["severity"] == "Critical"
    assert flag["id"] == "TEST"


def test_plan_test_notifications_makes_no_db_write():
    with patch("db.state_store.get_config", return_value=""), \
         patch("db.state_store.find_pm", return_value=None), \
         patch("db.state_store.create_notification") as mock_create:
        rows = plan_test_notifications()
    mock_create.assert_not_called()
    assert rows


def test_preview_never_sends(monkeypatch):
    monkeypatch.delenv("CASEY_ENV", raising=False)
    with patch("db.state_store.get_config", side_effect=lambda k: {"casey_slack_test_channel": "test-alerts"}.get(k, "")), \
         patch("db.state_store.get_pm_list", return_value=[]), \
         patch("integrations.slack.send_message") as mock_slack, \
         patch("integrations.gmail.send_email") as mock_email:
        rows = [{"channel": "slack_channel", "recipient": "urgentmatters"}]
        previewed = preview(rows)
    mock_slack.assert_not_called()
    mock_email.assert_not_called()
    assert previewed[0]["would_send_to"] == "test-alerts"
