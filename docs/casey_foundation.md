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
