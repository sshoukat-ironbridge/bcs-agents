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

### Dashboard auth audit (Step 7j)

Today's auth is **not** a single shared credential: `dashboard_users` is a
real per-account table (email + PBKDF2 password hash) with server-side
sessions in `dashboard_sessions` (`dashboard/auth.py`), login/logout/change
password all working per-user. There is no `create_user` script in the repo
— accounts are added by hand — and, more importantly, **no role concept
exists at all**: every logged-in account currently has identical, full
access (the login middleware only checks "is there a valid session," never
"what can this user see").

Given that, added a `role` column (default `'admin'`) and a `linked_name`
column directly on the existing `dashboard_users` table, rather than a new
table, so every account that could already log in keeps working exactly as
before (full access, role `admin`) with zero migration needed for existing
rows. No UI yet; `agents/casey/access.py` (Step 8a) is the role filter this
sets up for.

### Schema (Step 7)

New tables (all via `Base.metadata.create_all`, confirmed on both a fresh
DB and the existing local DB): `customer_messages`, `message_analyses`,
`casey_flags`, `casey_flag_events`, `casey_notifications`,
`casey_response_watches`, `ingest_cursors`, `webhook_events`.

- `casey_flags.dedup_key` has a **partial unique index** (`WHERE closed_at
  IS NULL`) so only one non-closed flag can exist per dedup key at a time;
  a closed flag never blocks a fresh one for the same underlying complaint.
- `casey_notifications.dedup_key` has a plain unique index (an unchanged
  record never alerts twice, regardless of open/closed state).
- `customer_messages` has `UNIQUE(source, external_id)` so re-ingesting the
  same Gmail/Dialpad item is a no-op.
- `job_key` is the composite `client_name|sheet_tab|primary_job_type`
  string (see "composite project key" decision below) — used everywhere in
  place of a real job identifier, since `casey_active_jobs` has no stable
  single-column ID that survives a client's job being re-keyed.
- Casey only reads `casey_active_jobs`; no column on it is owned by Casey
  except `address` (Step 7a, Part 1).
- New `app_config` defaults (all empty/off by default): `casey_email_ana_maria`,
  `casey_email_tatiana`, `casey_email_chris`, `casey_email_override`,
  `dialpad_webhook_secret`, `casey_response_window_hours` ("24"),
  `casey_sms_counts_as_response` ("false").

### Pure logic modules (Step 8)

All under `agents/casey/`, no network/DB access, full unit test coverage:

- **`access.py`**: `visible_projects(role, linked_name, projects)` — admin/
  leadership/cx_coordinator see everything; pm/estimator see only projects
  where `pm_name`/`estimator_name` matches `linked_name`; any other role
  fails closed (sees nothing).
- **`flag_logic.py`**: `decide_action(existing_open_flags, candidate)` →
  one of `create | escalate | note_improvement | no_op | create_separate`.
  Same complaint = same `job_key` + same `primary_reason` (configurable via
  `same_complaint_by`). Escalates only on a severity *increase*; improving
  sentiment is noted but never auto-closes or auto-lowers a flag (humans
  only, with an audit event elsewhere).
- **`notification_plan.py`**: `plan_notifications(flag, action, ...)` plans
  (never sends) a Slack channel post + PM DM for every plannable action,
  plus emails per `DEFAULT_EMAIL_MATRIX` (Low: PM+Ana Maria; Medium: +
  Tatiana; High/Critical: + Chris — **doc ambiguity**: the doc's prose says
  Tatiana joins at High, its table says Medium; defaulted to the table,
  overridable via the `email_matrix` param). `casey_email_override`
  replaces every email recipient when set. `dedup_key` = `flag_id:severity`
  so an unchanged flag never re-alerts and an escalation always gets a
  fresh notification.
- **`response_watch.py`**: `classify_response(inbound, candidates, now, ...)`
  → `waiting | responded | overdue | excluded`. A response is an outbound
  email in the same thread, or a *connected* (duration > 0) outbound call
  with that customer, within `casey_response_window_hours` (default 24)
  consecutive calendar hours of `received_at`. SMS counts only when
  `casey_sms_counts_as_response` is true (default false). Automated inbound
  mail is excluded outright.
- **`normalizers.py`** (fixtures in `tests/fixtures/casey/`):
  - `normalize_dialpad_transcript(call, transcript)`: PM lines (`user_id`)
    vs. customer lines (`contact_id`); "moment" lines excluded from the
    text body but kept in `raw_ref`; naive-UTC Dialpad timestamps get
    `tzinfo=UTC` attached explicitly; duration is read as-is in
    milliseconds; a zero-line transcript returns `None` (skipped, not an
    empty row).
  - `normalize_gmail_message(message)`: direction by sender domain
    (`OUR_EMAIL_DOMAIN = "bluecollarscholars.net"`, matching the address
    already hardcoded in `db/state_store.py`'s config defaults);
    `is_automated_email()` checks Auto-Submitted, bulk/list/junk
    Precedence, List-Unsubscribe/List-Id, X-Autoreply, noreply-style
    sender local parts, and the `[BCS Update]` subject prefix.
  - `normalize_dialpad_sms(payload)`: **checked against Dialpad's actual
    docs** (https://developers.dialpad.com/docs/sms-events.md, fetched
    directly) rather than guessed — confirmed fields are `id`, `direction`,
    `created_date` (ms epoch), `from_number`, `to_number` (list), `text`,
    and `contact.phone_number` (the external party, regardless of
    direction). The same lookup found that **the webhook payload itself is
    JWT-encoded**, signed with a secret chosen when the webhook is created
    (not returned by the API) — this reshapes Step 9's signature
    verification; see the webhook section below.
  - `build_contact_index()` / `match_job()` / `count_ambiguous_contacts()`:
    matches a message to a job by customer email first, then phone; more
    than one job sharing a contact never auto-matches (`job_key: None`,
    every candidate listed) — never guesses. Against the live local DB
    (176 active jobs): **8 ambiguous emails, 7 ambiguous phones**.
- **`ingest_windows.py`**: `next_window()` picks the next fetch window
  (first run: `default_lookback_hours` back from now; otherwise
  `overlap_minutes` before the last cursor, so a late-arriving event near
  the previous window's edge is re-fetched rather than missed — safe
  because every source dedupes on `(source, external_id)`).
  `paginate()` loops a caller-supplied `fetch_page` to a hard `max_pages`
  cap, so a pagination bug or runaway feed can't loop forever; it makes no
  network calls itself.

### Taxonomy (Step 6)

`agents/casey/taxonomy.py` holds the severities, statuses, reasons and root
causes verbatim from Chris's v1.3 doc, plus:
- `severity_rank()` for ordered comparison (Low < Medium < High < Critical).
- `SEVERITY_GUIDE`: the interpretive text per severity, for later prompts.
- `validate_reasons()`: enforces "Other (explain in Notes)" requiring
  non-empty notes, whether it's the primary reason or among the additional
  reasons.
- `default_root_cause()`: always `"Undetermined (default)"`.
