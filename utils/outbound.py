"""The single gate every outbound send (Slack, email, DM) must check.

Returns False — meaning "do not actually send" — when either:
  - app_config silent_mode is "true", or
  - the OCA_DRY_RUN=1 environment variable is set.

This replaces the old pattern where each integration function (and each
caller) separately checked silent_mode on its own — a gap that let the
deadline-change Slack alert ignore OCA_DRY_RUN entirely, since it only
ever checked silent_mode. Every send-capable function in integrations/
slack.py and integrations/gmail.py, plus agents/oca/alerts.py and
agents/oca/checklist.py, now call this instead.
"""
import os

from db.state_store import get_config


def outbound_enabled() -> bool:
    if os.getenv("OCA_DRY_RUN", "").strip() == "1":
        return False
    return (get_config("silent_mode") or "true").strip().lower() != "true"
