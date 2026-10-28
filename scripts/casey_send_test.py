"""Creates a synthetic Critical flag in memory (never written to the DB),
plans its notifications, and prints exactly what WOULD be sent to whom
after the override/test-channel rules resolve. With --yes, sends it
through the real gate (agents.casey.notify.send_one) — safe to do
locally because that gate only ever reaches override/test-channel
recipients when CASEY_ENV is unset.

Usage:
    python scripts/casey_send_test.py            # dry preview only
    python scripts/casey_send_test.py --yes       # actually sends, locally only
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("casey.send_test")


def build_synthetic_flag() -> dict:
    return {
        "id": "TEST",  # never a real flag id — this flag is never written to the DB
        "severity": "Critical",
        "job_key": "TEST|to_start|SendTest",
        "customer_name": "Test Customer (synthetic)",
        "address": "123 Test Lane",
        "pm_name": "Test PM",
        "primary_reason": "Safety concern",
        "issue_summary": "Synthetic Critical flag created by casey_send_test.py for a local send check.",
    }


def plan_test_notifications() -> list[dict]:
    from db.state_store import get_config, find_pm
    from agents.casey.notification_plan import plan_notifications, RECIPIENT_CONFIG_KEYS

    flag = build_synthetic_flag()
    recipient_addresses = {key: (get_config(cfg_key) or "") for key, cfg_key in RECIPIENT_CONFIG_KEYS.items()}
    recipient_addresses["override"] = get_config("casey_email_override") or ""
    pm = find_pm(flag.get("pm_name") or "")
    pm_email = (pm or {}).get("email", "")
    return plan_notifications(flag, "create", recipient_addresses=recipient_addresses, pm_email=pm_email)


def _env() -> dict:
    from db.state_store import get_config, get_pm_list

    return {
        "test_channel": get_config("casey_slack_test_channel") or "",
        "email_override": get_config("casey_email_override") or "",
        "pm_slack_user_ids": {pm["full_name"]: pm.get("slack_user_id", "") for pm in get_pm_list()},
    }


def preview(rows: list[dict]) -> list[dict]:
    from agents.casey.notify import resolve_target

    env = _env()
    previewed = []
    for row in rows:
        target, ok, note = resolve_target(
            row, test_channel=env["test_channel"], email_override=env["email_override"],
            pm_slack_user_ids=env["pm_slack_user_ids"],
        )
        previewed.append({"channel": row["channel"], "configured_recipient": row["recipient"],
                           "would_send_to": target, "would_send": ok, "note": note})
    return previewed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Actually send through the real gate.")
    args = parser.parse_args()

    rows = plan_test_notifications()
    previewed = preview(rows)

    print("Synthetic Critical flag — notifications that WOULD be sent:")
    for p in previewed:
        if p["would_send"]:
            print(f"  [{p['channel']}] {p['configured_recipient']} -> {p['would_send_to']}: WOULD SEND")
        else:
            print(f"  [{p['channel']}] {p['configured_recipient']}: SKIPPED ({p['note']})")

    if not args.yes:
        print("\n(dry preview only — pass --yes to actually send through the real gate)")
        return

    from db.state_store import get_config

    if (get_config("silent_mode") or "true").strip().lower() == "true":
        print("ERROR: silent_mode is true — set it false before using --yes.", file=sys.stderr)
        sys.exit(1)
    if os.getenv("CASEY_ENV", "").strip():
        print("ERROR: CASEY_ENV is set — this script only sends locally when CASEY_ENV is unset.", file=sys.stderr)
        sys.exit(1)

    from agents.casey.notify import send_one

    env = _env()
    print("\nSending:")
    for row in rows:
        ok, error = send_one(
            row, test_channel=env["test_channel"], email_override=env["email_override"],
            pm_slack_user_ids=env["pm_slack_user_ids"],
        )
        print(f"  [{row['channel']}] {row['recipient']}: {'sent' if ok else f'FAILED ({error})'}")


if __name__ == "__main__":
    main()
