"""Prints which Casey prerequisites are configured, without calling any
external API. Never prints secret values — only "set" or "not set".

Usage:
    docker compose run --rm -e POSTGRES_URL=... oca python3 scripts/casey_readiness.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


def _status(label: str, is_set: bool, extra: str = "") -> str:
    state = "set" if is_set else "NOT SET"
    suffix = f" ({extra})" if extra else ""
    return f"  [{'OK' if is_set else '--'}] {label}: {state}{suffix}"


def _check_config() -> None:
    from db.state_store import get_config

    print("=== Configuration ===")

    dialpad_key = (get_config("dialpad_api_key") or "").strip()
    print(_status("Dialpad API key", bool(dialpad_key)))

    sa_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sa_extra = ""
    if sa_env and not sa_env.startswith("{"):
        sa_extra = "file found" if os.path.isfile(sa_env) else "path does not exist"
    print(_status("Gmail service account (GOOGLE_SERVICE_ACCOUNT_JSON)", bool(sa_env), sa_extra))

    urgentmatters = (get_config("slack_urgentmatters_channel") or "").strip()
    print(_status("Slack urgentmatters channel", bool(urgentmatters)))

    webhook_secret = (get_config("dialpad_webhook_secret") or "").strip()
    print(_status("Dialpad webhook secret", bool(webhook_secret)))

    for key in ("casey_email_ana_maria", "casey_email_tatiana", "casey_email_chris"):
        print(_status(f"Recipient config: {key}", bool((get_config(key) or "").strip())))
    override = (get_config("casey_email_override") or "").strip()
    print(_status("casey_email_override (local testing)", bool(override)))

    window_hours = (get_config("casey_response_window_hours") or "").strip()
    print(_status("Response window hours", bool(window_hours), window_hours or "default 24"))

    sms_counts = (get_config("casey_sms_counts_as_response") or "").strip()
    print(_status("SMS counts as response", bool(sms_counts), sms_counts or "default false"))


def _check_tables() -> None:
    from db.state_store import _db_available, _Session

    print("\n=== Tables ===")
    if not _db_available:
        print("  Database not available.")
        return

    tables = [
        "casey_active_jobs", "customer_messages", "message_analyses",
        "casey_flags", "casey_flag_events", "casey_notifications",
        "casey_response_watches", "ingest_cursors", "webhook_events",
    ]
    from sqlalchemy import text
    with _Session() as s:
        for table in tables:
            exists = s.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"), {"t": table}
            ).fetchone()
            if not exists:
                print(f"  [--] {table}: MISSING")
                continue
            count = s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            print(f"  [OK] {table}: {count} rows")


def _check_job_data_completeness() -> None:
    from db.state_store import get_all_active_jobs

    print("\n=== Active job data completeness ===")
    jobs = get_all_active_jobs()
    total = len(jobs)
    missing_pm = sum(1 for j in jobs if not (j.get("pm_name") or "").strip())
    missing_estimator = sum(1 for j in jobs if not (j.get("estimator_name") or "").strip())
    missing_email = sum(1 for j in jobs if not (j.get("customer_email") or "").strip())
    missing_phone = sum(1 for j in jobs if not (j.get("customer_phone") or "").strip())
    missing_address = sum(1 for j in jobs if not (j.get("address") or "").strip())

    print(f"  Total active jobs: {total}")
    print(f"  Missing PM: {missing_pm}")
    print(f"  Missing estimator: {missing_estimator}")
    print(f"  Missing email: {missing_email}")
    print(f"  Missing phone: {missing_phone}")
    print(f"  Missing address: {missing_address}")


def main() -> None:
    _check_config()
    _check_tables()
    _check_job_data_completeness()


if __name__ == "__main__":
    main()
