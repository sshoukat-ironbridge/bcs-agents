# Casey foundation

Work-in-progress log for the Casey rebuild on branch `new-features`. Local only:
no pushes, no production DB, no live Slack/email sends, `silent_mode` stays
`"true"` throughout. All new datetimes are timezone-aware UTC; display goes
through `utils/timeutil.py` (America/New_York).

## Part 1: DB cleanup

### Audit (Step 1)

Dead (removed in Step 2 — no reader besides their own model definition and
the "clear job data" delete routes):
- Tables: `dialpad_contact_history`, `customer_email_history`,
  `casey_sent_alerts`, `oca_runs`.
- `CaseyActiveJob` columns: `hubspot_deal_id`, `hubspot_contact_id`,
  `hubspot_owner_name`, `last_customer_update_sent`, `next_scheduled_update`,
  `escalation_flag`, `escalation_reason`, `last_email_thread_id`,
  `last_email_message_id`, `last_email_subject`.
- `app_config` keys `hubspot_*`, `casey_emails_paused*` — none existed in the
  DB defaults or anywhere in code; nothing to delete.
- `integrations/hubspot.py` — already absent from an earlier cleanup stage.

Kept (live readers found):
- `CaseyActiveJob` (the table) — this is OCA's real active-jobs table despite
  the legacy "Casey" name; read/written by `agents/oca/main.py`,
  `dashboard/routes/jobs.py`, `db/state_store.py`. New Casey reads it
  read-only.
- `SalesRepConfig.hubspot_owner_id` — live, used by
  `dashboard/routes/config.py` + `dashboard/templates/config.html` for the
  sales-rep management form (unrelated to any HubSpot API integration).
- `CaseyActiveJob.comms_hold*`, `.retired*` — live, used by OCA.
- `integrations/quickbooks.py` — live: `dashboard/routes/config.py`'s
  QuickBooks OAuth-connect route imports `exchange_code_for_tokens` from it.
  It only uses `requests` internally, not the `python-quickbooks` PyPI
  package, so the file stays but the PyPI package is dead weight.

### Decisions and assumptions

- **`get_summary()["total"]` kept its old meaning** (every `casey_active_jobs`
  row, including retired) rather than switching to a non-retired filter,
  to avoid an incidental behaviour change outside this cleanup's scope. The
  home-page "Total Active Jobs" stat card's literal count is therefore
  unchanged from before the cleanup.
- **Dropped the "Due for Update" dashboard stat card** along with
  `escalation_flag`/`next_scheduled_update`: it was an old-Casey "due for
  update" reminder concept, not part of the new Casey v1.3 taxonomy, and had
  no other reader.
- **`CaseyActiveJob.address`** (new nullable `Text` column, Step 7a): sourced
  from `integrations/sheets.py`'s existing `address` field, saved on every
  `upsert_active_job()` call (insert and update paths), with a unit test
  (`tests/test_state_store_jobs.py`).

### Cleanup script and backup procedure (Step 4)

`scripts/db_cleanup.py` drops the dead tables/columns/`app_config` keys
listed above. It is never called by `init_db()` or any agent; a human runs
it directly, and the default is a dry run.

Backup before applying:
```
docker compose exec -T postgres pg_dump -U <user> <db> > backups/<name>.sql
```
`./backups/` is gitignored.

Apply (after taking the backup):
```
ALLOW_DB_CLEANUP=1 python scripts/db_cleanup.py --apply --backup-file backups/<name>.sql
```
Requires all three: `ALLOW_DB_CLEANUP=1`, an existing non-empty
`--backup-file`, and typing the database name (read from `POSTGRES_URL`) to
confirm. Runs as one transaction using `DROP TABLE IF EXISTS` /
`ALTER TABLE ... DROP COLUMN IF EXISTS`, so it's idempotent (safe to re-run).

### Local apply (Step 5) — verified

Ran against the local DB: backup taken (`backups/pre_cleanup.sql`), dry run
printed real dead data (4 `dialpad_contact_history` rows, 54
`customer_email_history` rows, 67 non-null `hubspot_owner_name` values, 182
non-null `escalation_flag` values, and a full set of `hubspot_*`/
`casey_emails_paused*` app_config keys), `--apply` dropped them all in one
transaction, and a re-run of the dry run reported everything "already
absent"/"none found" (idempotent). Two back-to-back OCA dry runs afterward
(`OCA_DRY_RUN=1`, `silent_mode=true`) produced identical section counts and
checklist content to each other and to the pre-cleanup checklist (same
section composition and line formats; absolute counts drift slightly
run-to-run only because of ordinary deadline/retirement churn, not the
cleanup). Full test suite: 82 passed.

## Part 2: Casey foundations

### Taxonomy (Step 6)

`agents/casey/taxonomy.py` holds the severities, statuses, reasons and root
causes verbatim from Chris's v1.3 doc, plus:
- `severity_rank()` for ordered comparison (Low < Medium < High < Critical).
- `SEVERITY_GUIDE`: the interpretive text per severity, for later prompts.
- `validate_reasons()`: enforces "Other (explain in Notes)" requiring
  non-empty notes, whether it's the primary reason or among the additional
  reasons.
- `default_root_cause()`: always `"Undetermined (default)"`.
