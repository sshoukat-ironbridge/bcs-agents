# Casey foundation

Work-in-progress log for the Casey rebuild on branch `new-features`. Local only:
no pushes, no production DB, no live Slack/email sends, `silent_mode` stays
`"true"` throughout. All new datetimes are timezone-aware UTC; display goes
through `utils/timeutil.py` (America/New_York).

> **`casey_active_jobs` is the shared jobs table**: OCA writes it (sheet
> sync, retire/reconcile), Casey reads it read-only. The table/model name
> is a historical leftover from before this rebuild — it is **not**
> renamed, to avoid an unnecessary migration across every OCA write path.

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

### Webhook receiver (Step 9)

`POST /webhooks/dialpad/sms` (`dashboard/routes/webhooks.py`), exempt from
login via `dashboard/main.py`'s `_PUBLIC_PREFIXES += ("/webhooks/",)`.
Reads the raw body (capped at 64KB — oversized bodies get `413`),
decodes/verifies the JWT against `dialpad_webhook_secret`, stores the
result in `webhook_events` (`signature_valid` recorded either way), and
returns `200` immediately — no event processing happens in the receiver.
No secret configured → `401`, unless `ALLOW_UNSIGNED_WEBHOOKS=1` (local dev
only), which accepts and stores plain JSON with `signature_valid=False`.

**Signing algorithm is UNCONFIRMED**: Dialpad's docs confirm the payload is
JWT-encoded and signed with a secret you choose yourself (not returned by
their API), but don't name the algorithm. `HS256` is assumed (the standard
choice for a shared-secret JWT) — flagging this as needing confirmation
from Dialpad support or a real captured webhook before going live.

Verified live: rebuilt the dashboard image, curled `/`, `/jobs` and
`/login` (login still enforced — `302`/`302`/`200`), posted an unsigned
body to the webhook with no secret configured (`401`), set
`dialpad_webhook_secret` in the local DB, posted a locally-signed JWT
(`200`), and confirmed the row landed in `webhook_events` with
`signature_valid = true` and the decoded payload. 7 new tests (172 total
passing) cover valid/invalid signature, missing secret (with and without
`ALLOW_UNSIGNED_WEBHOOKS`), oversized body, and that every other dashboard
route still requires login.

### Calibration golden set (Step 10)

`tests/fixtures/casey/calibration.json`: 9 cases from Chris's examples
(verbatim quotes, except the Sandstrom pre-cancellation entry, which is
explicitly marked `"boundary_note"` containing "summary, not a verbatim
quote" — Carl's actual words were never invented). `agents/casey/
calibration.py::load_calibration_set()` validates the file shape and that
every `expected_severity` is one of `SEVERITIES`. No LLM calls yet; this
set is for evaluating the severity-classification prompt once it exists.

### Readiness check (Step 11)

`scripts/casey_readiness.py` prints configuration status (set/not set,
never values), table existence + row counts, and active-job data gaps —
no external API calls. Run live against the local DB: Dialpad API key,
Gmail service account, and Slack channel are configured; the webhook
secret and all three recipient emails are not (expected locally — never
hardcoded). All 9 Casey tables exist. Of 176 active jobs: 32 missing PM,
0 missing estimator, 18 missing email, 21 missing phone, and 176 missing
address (expected — no sync has run since the `address` column was added
in Step 7a; the next OCA sync backfills it from the sheet).

## Summary (Step 12)

### Schema overview

Casey reads `casey_active_jobs` (OCA's table) **read-only**, and owns nine
tables of its own: `customer_messages`, `message_analyses`, `casey_flags`,
`casey_flag_events`, `casey_notifications`, `casey_response_watches`,
`ingest_cursors`, `webhook_events`, plus a `role`/`linked_name` extension
to the existing `dashboard_users` table. See "Schema (Step 7)" above for
the dedup/uniqueness details.

### Pure modules

`agents/casey/`: `taxonomy.py` (constants), `access.py` (role visibility),
`flag_logic.py` (dedupe/escalate decision), `notification_plan.py` (plan,
never send), `response_watch.py` (24h window classification),
`normalizers.py` (Dialpad/Gmail → row dicts, job matching),
`ingest_windows.py` (fetch window + capped pagination), `calibration.py`
(golden-set loader). All DB/network-free, all unit tested.

### Decisions and assumptions (consolidated)

- **Tatiana's email tier**: doc prose says High, doc table says Medium.
  Defaulted to the table (Medium, High, Critical all include Tatiana),
  configurable via `notification_plan.plan_notifications`'s `email_matrix`
  parameter. (Step 8c)
- **Response window**: 24 **consecutive calendar hours**, not business
  hours, configurable via `casey_response_window_hours` (default `"24"`).
  (Step 8d)
- **Composite project key**: `job_key` = `client_name|sheet_tab|
  primary_job_type` everywhere (messages, flags, notifications, watches),
  because `casey_active_jobs` has no single stable ID that survives a
  client's job being re-keyed. Used until BCS has a real per-job unique ID.
  (Step 7)
- **Permits tab**: `integrations/sheets.py::get_active_jobs()` only reads
  the "To Start" and "In Process" tabs — there is no "Permits" tab in the
  live sheet today, so it's skipped (pre-existing behavior, confirmed
  while building the Casey schema, not changed).
- **Webhook signature method**: confirmed JWT + shared secret (Dialpad's
  own docs); the **signing algorithm (HS256) is assumed, not confirmed**.
  (Step 9)
- **SMS as a qualifying response**: off by default
  (`casey_sms_counts_as_response = "false"`), opt-in via config. (Step 8d)
- **Recipient email defaults**: `casey_email_ana_maria`,
  `casey_email_tatiana`, `casey_email_chris` all default to **empty
  string** — never a real address in code or tests — plus
  `casey_email_override` to redirect every email locally. (Step 8c)
- **`get_summary()["total"]`**: kept counting every row (retired included)
  rather than changing to a non-retired filter, to avoid an incidental
  behaviour change outside the cleanup's scope. (Step 2, Part 1)
- **Dashboard roles added to the existing `dashboard_users` table**, not a
  new table, since auth there was already per-account (not a shared
  credential) and just lacked a role concept; existing accounts default to
  `role = 'admin'` so nothing that could already log in loses access.
  (Step 7j)

### Still needs confirmation

- **Dialpad webhook JWT signing algorithm** — assumed HS256; confirm with
  Dialpad support or a real captured webhook before relying on it in
  production.
- **Dialpad SMS webhook field names** beyond what's implemented — the
  normalizer matches the documented schema exactly
  (https://developers.dialpad.com/docs/sms-events.md), but that page could
  change; re-check before go-live.
- **Tatiana's actual email tier** (Medium vs. High) — needs a decision
  from Chris; currently defaults to the doc's table.
- **`dashboard_users.role` values for existing accounts** — all default to
  `admin` on migration; whoever owns user management should confirm which
  real accounts should be reassigned to `pm`/`estimator`/`leadership`/
  `cx_coordinator` (no UI for this yet).
- **Ambiguous contacts** (8 emails, 7 phones shared by more than one job
  locally) will never auto-match once ingestion is built — needs a human
  process (or sheet data cleanup) for those specific clients.

## Part 3: Stage 5 — Dialpad call ingestion (read only)

### Review fixes (Step 0)

- **`dashboard_users.role` default was backwards**: the column defaulted
  new rows to `'admin'`, which only made sense for the one-time backfill
  of accounts that existed before roles existed. Fixed: existing rows are
  backfilled to `'admin'` once (`UPDATE ... WHERE role IS NULL`), then the
  column default changes to `'pm'` (least privileged) going forward.
  `db/state_store.py::create_user()` is new and **requires an explicit
  role** (no default parameter) — `agents/casey/access.py::
  DEFAULT_NEW_USER_ROLE` (`"pm"`) is what any future creation form should
  pre-select, not something the function assumes on its own. Verified live:
  the existing `admin@gmail.com` row kept `role=admin` after migration, the
  column's `DEFAULT` is now `'pm'`, and `create_user(..., role="pm")`
  stores correctly.
- **Webhook receiver no longer stores anything for an unverified
  signature**: `webhook_events` gets a row ONLY when the JWT verifies.
  An invalid/unsigned/unconfigured-secret request is never persisted —
  only a `WARNING` log line (IP, body size, running per-IP count) and an
  in-memory counter. Added a per-IP sliding-window rate limiter
  (`SlidingWindowRateLimiter`, `casey_webhook_rate_limit_per_minute`,
  default `"30"`), returning `429` over the limit.
- **DB vs. sheet row-count check**: active `casey_active_jobs` rows per
  tab vs. distinct `(client_name, sheet_tab, primary_job_type)` keys in
  the current sheet pull matched exactly (`in_process: 71=71`,
  `to_start: 105=105`) — no divergence to explain. (The raw sheet fetch
  returned 179 rows; 176 distinct keys is 3 duplicates, matching OCA's
  existing "Duplicate sheet rows" checklist section.)
- **Note added** at the top of this doc: `casey_active_jobs` is the
  shared table (OCA writes, Casey reads), not renamed.

### Dialpad API re-verification (Step 1)

Fetched directly from https://developers.dialpad.com (not guessed, not
relying on the earlier probe):

**`GET /api/v2/call` — confirmed:**
- `started_after` / `started_before`: int64, UTC **milliseconds** since
  epoch.
- `target_id` (int64) + `target_type` (must be `"user"` for a PM):
  `target_type` is required whenever `target_id` is given.
- `cursor` (string): pagination token from the previous response's
  top-level `cursor` field; response also has `items` (array).
- `include_anonymized` (bool, default false).
- **No page-size/`limit` parameter is documented at all** — pagination is
  cursor-only; our page cap is a client-side loop limit, not a server
  parameter.
- Call object fields: `call_id` (int64), `direction`, `state` (string,
  no documented enum — only a `"connected"` example value), `duration`
  and `total_duration` (both milliseconds), `date_started`/`date_connected`/
  `date_ended` (int64 timestamps), `contact`/`target` objects (each with
  `id`, `name`, `phone`, `email`), `external_number`/`internal_number`.
- Rate limit: 1200 requests/minute (documented).

**`GET /api/v2/transcripts/{call_id}` — confirmed:**
- Response: `{"call_id": int64, "lines": [...]}`.
- Each line: `type` (`"moment"` | `"transcript"` | also `ai_question`,
  `custom_moment`, `real_time_moment`, or null), `content` (string),
  `time` (ISO datetime, **no timezone suffix** — confirms the "attach
  UTC explicitly" assumption from Step 8e), `user_id` (int64, PM side),
  `contact_id` (string, customer side), `name`.

**Could not confirm from the docs:**
- The full enum of `state` values (only `"connected"` is shown as an
  example) — using `state == "connected"` AND `duration_ms > 0` together
  as the "actually connected" signal, per the task's own wording, rather
  than trusting `state` alone.
- What a "transcript not ready yet" response looks like — only a `200`
  is documented. Operationally this doesn't matter: a 0-line response is
  treated as "no transcript" either way (logged, not stored), and the
  ingestion window's overlap re-fetches it next run regardless of whether
  it was genuinely empty or just not ready yet.

### Live check against real Dialpad data (Step 6) — 3 real bugs caught and fixed

Running the first `--dry-run` against the actual API caught three bugs
that unit tests (mocking Dialpad's responses) couldn't have caught, since
the mocks were built on the same wrong assumptions as the code:

1. **`state` is never `"connected"`** — Dialpad's one documented example
   value for a concluded call is misleading; every real call sampled
   (250+) had `state == "hangup"`. Fixed `_is_connected()` to use
   `duration > 0` alone (confirmed to correlate exactly with
   `date_connected` being set across every call sampled).
2. **`call.date_started` is an int64 ms-epoch timestamp**, not a naive
   ISO string like the transcript lines' `time` field — conflating the
   two raised `ValueError: day is out of range for month` on the very
   first real call. Added `_parse_epoch_ms()` and switched
   `normalize_dialpad_transcript()` to use it for `date_started` (the
   transcript lines still use `_parse_utc_naive()` for their own `time`
   field, which genuinely is a naive ISO string).
3. **Field names were wrong**: the call's unique ID field is `call_id`,
   not `id`; the contact's phone field is `phone`, not `phone_number`.
   Both were written before Step 1's doc re-verification and never
   caught until live data exercised them.

All three are now fixed, covered by regression-shaped fixtures/tests, and
confirmed against two full `--dry-run` passes plus two real ingestion
runs. `contact.type == "user"` (the internal-call signal) WAS confirmed
live: 69 of 478 calls had `contact.type == "user"` across the sample
(other contacts were typed `"local"`).

Also fixed while here: `agents/casey/normalizers.py::_normalize_phone()`
now keeps only the **last 10 digits** (was: all digits), so a number with
a leading `"+1"` country code matches the same job's number without it —
this changes `match_job()`/`build_contact_index()` everywhere, not just
Dialpad calls. Re-ran `count_ambiguous_contacts()` against the live local
DB after the fix: **8 ambiguous emails (unchanged), 9 ambiguous phones
(was 7)** — the 2 additional phones were previously hidden by a
formatting/country-code difference that now correctly collapses to the
same number.

**Live summaries** (`--since 7d`, local DB, real Dialpad API):

| Run | Window | Calls seen | With transcript | Empty | Internal | Not connected | Matched | Ambiguous | Unmatched | New rows |
|---|---|---|---|---|---|---|---|---|---|---|
| dry-run | 7d | 478 | 329 | 19 | 69 | 61 | 35 | 1 | 293 | 0 (dry run) |
| real #1 | 7d | 480 | 330 | 20 | 69 | 61 | 35 | 1 | 294 | **330** |
| real #2 | ~2h12m (overlap only) | 29 | 17 | 1 | 9 | 2 | 2 | 0 | 15 | **0** |

Per-PM call counts (real #1, 7 days): Gustavo 4, Laura 20, Julie 10,
Daniel 72, Andrea 63, Esteban 24, Carlos 57, Antonio 45, Fernanda 114,
Jorge 0, Santiago 71. **0 PMs skipped** for a missing `dialpad_user_id`
— all 11 configured PMs have one. Verified directly against the DB:
`customer_messages` has exactly 330 `dialpad_call` rows after both real
runs (unchanged by run #2), and `ingest_cursors` advanced to the end of
run #2's window.

The high unmatched count (293/330, ~89%) looks consistent with a PM's
real phone line fielding plenty of calls unrelated to an active tracked
job (sales calls, wrong numbers, calls about jobs not currently in
`casey_active_jobs`) rather than a matching bug — `match_job()`'s own
unit tests confirm the matching logic itself is correct.
