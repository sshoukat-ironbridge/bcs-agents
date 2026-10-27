"""
Drops the dead legacy-Casey tables/columns identified in Part 1, Step 1 of
docs/casey_foundation.md. Default is a dry run (safe, read-only): it prints
each candidate table's row count, each candidate column's non-null count,
and any matching app_config keys, without changing anything.

--apply actually drops them, and requires ALL of:
  - env ALLOW_DB_CLEANUP=1
  - --backup-file pointing at an existing, non-empty backup file
  - typing the database name (from POSTGRES_URL) to confirm

Never invoked by init_db or any agent run — a human runs this directly.

Usage:
    python scripts/db_cleanup.py                                   # dry run
    ALLOW_DB_CLEANUP=1 python scripts/db_cleanup.py --apply \\
        --backup-file backups/pre_cleanup.sql

Take the backup first:
    docker compose exec -T postgres pg_dump -U <user> <db> > backups/pre_cleanup.sql
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text  # noqa: E402

# Candidates from docs/casey_foundation.md's audit — static, not user input.
DEAD_TABLES = ["dialpad_contact_history", "customer_email_history", "casey_sent_alerts", "oca_runs"]

DEAD_COLUMNS = {
    "casey_active_jobs": [
        "hubspot_deal_id", "hubspot_contact_id", "hubspot_owner_name",
        "last_customer_update_sent", "next_scheduled_update",
        "escalation_flag", "escalation_reason",
        "last_email_thread_id", "last_email_message_id", "last_email_subject",
    ],
}

DEAD_CONFIG_KEY_PATTERNS = ["hubspot_%", "casey_emails_paused%"]


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"), {"t": table}
    ).fetchone() is not None


def _column_exists(conn, table: str, column: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
        {"t": table, "c": column},
    ).fetchone() is not None


def _dead_config_keys(conn) -> list[str]:
    keys = []
    for pattern in DEAD_CONFIG_KEY_PATTERNS:
        rows = conn.execute(text("SELECT key FROM app_config WHERE key LIKE :p"), {"p": pattern}).fetchall()
        keys.extend(row[0] for row in rows)
    return keys


def report(conn) -> None:
    print("=== Tables that would be dropped ===")
    for table in DEAD_TABLES:
        if _table_exists(conn, table):
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            print(f"  {table}: {count} rows")
        else:
            print(f"  {table}: (already absent)")

    print("\n=== Columns that would be dropped ===")
    for table, columns in DEAD_COLUMNS.items():
        for column in columns:
            if _column_exists(conn, table, column):
                non_null = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NOT NULL")).scalar()
                print(f"  {table}.{column}: {non_null} non-null rows")
            else:
                print(f"  {table}.{column}: (already absent)")

    print("\n=== app_config keys that would be deleted ===")
    keys = _dead_config_keys(conn)
    if keys:
        for key in keys:
            print(f"  {key}")
    else:
        print("  (none found)")


def apply(conn) -> None:
    for table in DEAD_TABLES:
        conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        print(f"  dropped table {table} (if it existed)")

    for table, columns in DEAD_COLUMNS.items():
        for column in columns:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
            print(f"  dropped column {table}.{column} (if it existed)")

    for pattern in DEAD_CONFIG_KEY_PATTERNS:
        conn.execute(text("DELETE FROM app_config WHERE key LIKE :p"), {"p": pattern})
    print("  deleted matching app_config keys (if any)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually drop (default: dry run only).")
    parser.add_argument("--backup-file", help="Path to an existing, non-empty pg_dump backup (required with --apply).")
    args = parser.parse_args()

    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        print("ERROR: POSTGRES_URL is not set.")
        sys.exit(1)

    engine = create_engine(postgres_url)
    db_name = engine.url.database

    if args.apply:
        if os.getenv("ALLOW_DB_CLEANUP", "").strip() != "1":
            print("Refusing to apply: env ALLOW_DB_CLEANUP=1 is required.")
            sys.exit(1)
        if not args.backup_file:
            print("Refusing to apply: --backup-file is required.")
            sys.exit(1)
        if not os.path.isfile(args.backup_file) or os.path.getsize(args.backup_file) == 0:
            print(f"Refusing to apply: backup file {args.backup_file!r} does not exist or is empty.")
            sys.exit(1)

        print(f"About to permanently drop dead tables/columns from database {db_name!r}.")
        print(f"Backup confirmed present and non-empty: {args.backup_file}")
        typed = input(f"Type the database name ({db_name}) to confirm: ").strip()
        if typed != db_name:
            print("Confirmation did not match database name. Aborting, nothing changed.")
            sys.exit(1)

        with engine.begin() as conn:
            print("\nApplying cleanup (one transaction, idempotent):")
            apply(conn)
        print("\nDone.")
    else:
        with engine.connect() as conn:
            report(conn)
        print("\nThis was a dry run. Nothing was changed. Re-run with --apply "
              "(and ALLOW_DB_CLEANUP=1, --backup-file, and the typed confirmation) to actually drop these.")


if __name__ == "__main__":
    main()
