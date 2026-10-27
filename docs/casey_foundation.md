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

## Stage 6

### Part A: Dialpad match-rate diagnosis

**Sheet-side phone format** (174 active jobs' `customer_phone`): 159
clean single numbers, 0 with an extension, 1 with two numbers in one
cell (newline-separated, one marked `"(wrong)"` in the sheet itself — a
data-quality oddity, not a representative pattern), 3 noise (one is
literally a street address in the phone cell, two are placeholder text
`"n/a"`/`"no phone"`), 11 empty.

**Dialpad-side phone format** (295 stored unmatched calls): **100%
uniform `+1##########`** (11 digits, `+1` prefix) — zero parsing
anomalies on the Dialpad side either.

**Unmatched call analysis** (295 rows, `job_id IS NULL`):
- 49 distinct contact numbers (not 295 distinct customers — the same
  small set of numbers repeats).
- 31 of those 49 numbers appear in 2+ calls.
- 13 numbers appear across 2+ different PMs — likely shared
  vendor/subcontractor/supplier lines, not customer-specific numbers.
- 0 of the 49 numbers appear as a substring in any active job's
  `customer_email` or `client_name`.
- No secondary phone field exists at all — `integrations/sheets.py`
  only reads one `"phone number"` column — so "secondary phone" doesn't
  apply.
- 174 of 295 have a `Customer:` transcript line of 20+ words — real
  conversations, not short/junk calls.

**Per-PM** (`customer_messages.pm_name`, from `pm_config.full_name`):
Andrea 39/14m, Antonio 32/4m, Carlos 18/6m, Daniel 61/5m, Esteban
19/4m, Fernanda 98/1m, Gustavo 2/0m, Julie 9/1m, Laura 3/0m, Santiago
49/0m (`total/matched`).

**Secondary finding (not the root cause, but worth fixing eventually)**:
`pm_config.full_name` (e.g. `"Santiago"`, `"Daniel"`) and
`casey_active_jobs.pm_name` (sheet values, e.g. `"Santiago A"`,
`"Santiago P"`, `"Daniel Gomez Cortez"`) use different naming
conventions for the same person — there is no canonical PM-identity
mapping between the two. This doesn't affect phone-based call matching
(which never looks at PM name), but it does mean a "calls per PM" and a
"jobs per PM" table can't be joined directly without that mapping — the
per-PM job-coverage numbers above use the sheet's own `pm_name` values
and are reported separately from the call counts for this reason.

**Diagnosis: NOT a phone-parsing bug.** The sheet data is 91% clean
single numbers (no extensions, one rare multi-number cell), and every
unmatched Dialpad number arrives in a single uniform clean format with
no anomaly to fix. The unmatched calls are concentrated in a small set
of real, correctly-formatted phone numbers that simply aren't any
active job's stored `customer_phone` — consistent with vendor/
subcontractor/supplier lines (13 numbers shared across PMs), calls
about jobs not currently in `casey_active_jobs` (closed, not yet
entered, or with a stale/blank sheet phone cell), and ordinary
unrelated calls. Per the task's decision rule: **changed nothing in
the matching/normalizer code** — `_normalize_phone()`'s existing
last-10-digit behavior (Stage 5) is already correct and sufficient;
there was no multi-number-cell or extension failure mode to fix. No
backfill was run, since there is nothing for a backfill to correct.

**Sanitized real fixtures** (`tests/fixtures/casey/dialpad_real_*.json`,
Step 5): 3 real call/transcript pairs captured live from the local DB's
configured PMs, then sanitized (every phone/email/name/call-id/contact-id
replaced with a placeholder, transcript text replaced with bracketed
placeholders) while preserving every field, type and shape exactly —
`dialpad_real_call_connected.json` (external call, mixed transcript+
moment lines including several moment subtypes never seen in the
hand-written Stage 4 fixtures: `ai_csat_reboot_ineligible`,
`call_purpose_category`, `whole_call_summary`,
`whole_call_summary_fragment`, `ner`, `date`), `dialpad_real_call_
internal.json` (`contact.type == "user"`, confirming the internal-call
signal against real data), `dialpad_real_call_zero_duration.json` (no
transcript was ever fetched for a 0-duration call, confirmed live).

### Part B: Gmail ingestion (read only)

**Existing Gmail integration audit (Step 1):**
- **Mailbox impersonated**: whatever `notification_sender_email`
  (`app_config`) holds — via Google Workspace domain-wide delegation, the
  service account can impersonate *any* mailbox in the domain; this
  config key is simply which one the send path currently uses. Locally
  configured as `chris.s@bluecollarscholars.net`.
- **Config key**: `notification_sender_email`.
- **Scope**: `https://mail.google.com/` — Gmail's **full-access** scope
  (read, send, modify, delete), not a read-only scope. This is correct
  for the existing *send* path, but means `integrations/gmail_read.py`
  (Step 2) requests its own narrower `gmail.readonly` scope, so the
  read-only guarantee is enforced by the credentials themselves, not
  just by which methods the wrapper happens to expose.
- **Mailboxes to read** (new, Step 1): `casey_gmail_mailboxes`
  (`app_config`, JSON list), defaulting to **only** the currently
  configured `notification_sender_email` mailbox — never defaults to
  any PM's own mailbox.

**`integrations/gmail_read.py` (Step 2)**: exposes only
`list_messages()`/`get_message()`/`get_thread()`, internally calling
only `users().messages().list/.get` and `users().threads().get` — no
send/modify/delete/label method appears anywhere in the module
(enforced by a test that greps the module source). Timeouts (15s) via
an `AuthorizedHttp`-wrapped `httplib2.Http`; 429/5xx retried 3 tries
with 2s/5s backoff; logs counts and message/thread ids only, never
subject or body text.

**Scope — LIVE-CHECKED, downgraded from the plan**: tried the narrower
`gmail.readonly` scope first; it failed live with `unauthorized_client`
at token refresh — the service account's Google Workspace domain-wide
delegation is authorized **only** for `https://mail.google.com/` (the
scope the existing send path already uses), not `gmail.readonly`.
Falls back to that already-authorized scope; the read-only guarantee
is therefore enforced at the **code level** (this module never calls a
send/modify/delete/label method) rather than at the **credential**
level. Revisit if a Workspace admin ever authorizes `gmail.readonly`
for this client ID. Verified live against the real
`chris.s@bluecollarscholars.net` mailbox: `list_messages()` and
`get_message()` both work and return real headers/`internalDate`.

**Real-data bug caught before Step 3 was written**: Gmail's actual "On
... wrote:" quote attribution line wraps, with `wrote:` landing on its
own line (confirmed against a real reply:
`"On Tue, Oct 27, 2026 at 3:16 PM Tatiana BCS <...>\nwrote:\n"`) — a
single-line `^On .+ wrote:$` regex never matches real mail.
`agents/casey/normalizers.py::_strip_quoted_and_signature()` now looks
ahead up to 3 lines for `wrote:` once a line starts with `"On "`.

**`agents/casey/ingest_gmail.py` (Step 3)**: `run(since, dry_run=...)`,
one cursor per mailbox (`ingest_cursors`, source `gmail:<mailbox>`,
`casey_gmail_overlap_hours` default 2). Query is
`after:<epoch_s> before:<epoch_s> -in:spam -in:trash`. Customer address:
inbound = sender; outbound = the first To/Cc address that's a known
customer email. Automated messages (existing `is_automated_email()`
rules) are counted but never stored. A customer address matching
exactly one job gets `job_id`/`job_key`; matching 2+ jobs gets
`job_id: null` with every candidate key folded into `raw_ref` as
`candidate_job_keys`, and is still **stored** (counted `ambiguous`) —
only an address matching **zero** jobs is "unmatched" and governed by
`casey_gmail_store_unmatched` (default false, counted either way).
Idempotent via the same `upsert_customer_message()` used for Dialpad.

**Step 5 fixtures** (`tests/fixtures/casey/gmail_real_*.json`): built
from real confirmed Gmail API shapes (top-level
`id/threadId/labelIds/snippet/payload/sizeEstimate/historyId/
internalDate`; `payload.parts` for multipart), covering
multipart/alternative, HTML-only, a forwarded thread, an automated
vendor sender, a quoted reply, an outbound BCS-address message, and a
message whose From address matches two active jobs (ambiguous). 15 new
ingestion tests + 20 normalizer tests, 265 total passing.

**`agents/casey/run_watches.py` (Step 4)**: creates a
`casey_response_watches` row for every stored inbound, non-automated
customer email with none yet (`due_at = received_at +
casey_response_window_hours`), then re-evaluates every non-closed
(`waiting`/`overdue`) watch via the existing
`response_watch.classify_response()` against outbound Gmail messages in
the same thread and connected (`duration_ms > 0`) outbound Dialpad
calls for the same `job_key` (a shared `"job:<job_key>"` sentinel is
used in place of a real phone number for the call-candidate match,
since watches don't carry a phone — the same-job filtering already
happened in the DB query, `get_response_candidates()`). An overdue
watch plans (never sends) one notification via
`notification_plan.plan_notifications()` using a synthetic flag
(`severity="Medium"`, `primary_reason="Lack of communication / no
updates"` — a watch has no taxonomy classification of its own, so this
is a fixed, documented default), deduped on `watch:<id>:overdue` so a
watch is never re-notified. No notifications are ever sent from here —
rows are stored with `status="planned"` only.

**2 real bugs caught by the live check against the actual local DB
(Stage 6 Part B Step 6)**:
1. `casey_notifications`'s unique constraint was `dedup_key` alone —
   but `notification_plan.plan_notifications()` intentionally returns
   several rows (Slack channel + DM + one per email recipient) sharing
   ONE `dedup_key` per batch, so every row after the first in a real
   batch hit a `UniqueViolation`. Widened to
   `UNIQUE (dedup_key, channel, recipient)`: the same channel+recipient
   for an unchanged `dedup_key` still never double-inserts, but a
   batch's other channels/recipients now can. Migrated on the local DB
   (drop old constraint, add the new one).
2. `_evaluate_open_watches()` double-counted every newly-created watch
   on a real run: `_create_missing_watches()` already persists new
   watches via `create_response_watch()` before evaluation runs, so
   `get_open_response_watches()` already includes them — appending the
   in-memory `new_watches` list on top counted each one twice (first
   live run: 176 classifications for only 88 real watches). Fixed to
   only add `new_watches` to the evaluation set during a **dry run**
   (which persists nothing, so they're the only record of those
   messages); a real run relies solely on the DB query.

**Live summaries** (local DB, real mailbox): `ingest_gmail --since 7d
--dry-run`: 132 messages, 16 stored candidates, 89 unmatched, 27
automated, 0 ambiguous. Real run: 133 messages, **17 new rows**.
Rerun: 20 messages (overlap window only), **0 new rows** — confirmed
directly in Postgres (`customer_messages` holds exactly 17 `gmail`
rows, unchanged). Per-PM stored-email counts: Laura Peña 7, Daniel
Gomez Cortez 3, Andrea Ortega 2, Fernanda 1, Santiago P 1, blank
(unmatched-PM) 3. Customer emails seen vs. stored: 83 inbound + 50
outbound seen, 17 stored (candidates that matched a known customer
address and weren't automated).

`run_watches --dry-run`: 15 waiting, 8 responded, 65 overdue (0
excluded) — meaningful even though no watch existed yet, confirming
the dry-run fix. Real run: **88 new watches created**, same 15/8/65
split, 195 notification rows planned (65 overdue × 3 channels), all
`status="planned"`. Rerun: **0 new watches**, 15 waiting / 65 overdue
unchanged, 0 responded (the 8 that resolved on the first run are now
closed and correctly excluded from the open-watch count) — confirmed
directly in Postgres (88 total watches, 195 notification rows,
unchanged after the rerun).

## Stage 7

### Part A Step 1: fix watch source scoping, delete wrongly-created watches

**Investigation**: of 88 `casey_response_watches`, only 17 `gmail` rows
existed at all — printing watch count by inbound message source showed
**82 of 88 watches were created from `dialpad_call` inbound rows**, 6
from `gmail`. Root cause: `db/state_store.py::
find_unwatched_inbound_messages()` filtered on `direction == "inbound"`
and `is_automated == False` only — no `source == "gmail"` condition —
even though the 24-hour response-watch rule (Stage 6) is specifically
about customer emails; a Dialpad call is answered or not within the
call itself, not via a follow-up watch. Only one code path ever creates
a watch (`run_watches.py` → `find_unwatched_inbound_messages()`), so
this was the single place to fix.

**Fix**: added `CustomerMessage.source == "gmail"` to the filter.
Regression test added (`inspect.getsource()`-based, since this
function's real SQL filtering can't be exercised without a live DB in
this project's DB-free unit test suite — sqlalchemy isn't even
installed outside Docker).

**Cleanup on the local DB**: deleted the 82 wrongly-created watches and
the 195 notifications tied to them (all notifications happened to be
tied to the wrongly-created, dialpad-sourced overdue watches).
Confirmed: `customer_messages` has exactly 6 inbound, non-automated
`gmail` rows, matching the 6 remaining (correct) watches exactly
(4 responded, 2 waiting, 0 overdue, 0 excluded; 0 notifications left).
Re-ran `run_watches` for real afterward: 0 new watches (all 6 already
exist), counts unchanged.

### Part A Step 2: backfill suppression

`casey_live_since` (`app_config`, UTC ISO timestamp, default **unset**
i.e. empty string): any notification planned for an event (a message's
`occurred_at`, a watch's `due_at`, or a flag event's timestamp) before
`casey_live_since`, **or any time `casey_live_since` is unset**, is
stored `status="suppressed"`, `reason="backfill"` instead of
`"planned"`. A missing event time is treated the same as "before
go-live" — fail safe, never guess something is live.
`agents/casey/notification_plan.py::apply_backfill_suppression()`
implements this (pure, mutates the rows `plan_notifications()` built);
wired into `run_watches.py`'s overdue-notification path via
`ingest_windows.parse_cursor()` to parse the config value. New
`casey_notifications.reason` column (nullable text) added via
migration, verified on the local DB.

**Setting `casey_live_since` at go-live**: once Casey is ready to
actually notify people (even before real sending is implemented, since
"planned" vs. "suppressed" is what a human reviews), set the config key
to the current UTC time in ISO 8601 format, e.g. via the dashboard
config UI or directly:
```
docker compose run --rm oca python3 -c "
from db.state_store import set_config
from datetime import datetime, timezone
set_config('casey_live_since', datetime.now(timezone.utc).isoformat())
"
```
Everything with an event time before that moment (all of today's
backlog) stays `suppressed`/`backfill` forever; only genuinely new
events after that moment ever get `planned`.

### Part A Step 3: corrected counts

Watches: 4 `responded`, 2 `waiting`, 0 `overdue`, 0 `excluded` (6
total — all real `gmail` rows now; the DB cleanup in Step 1 removed
the 82 wrongly-created `dialpad_call`-sourced ones). Notifications: 0
rows (no watch is currently overdue to trigger one; the schema and
suppression logic are verified by unit tests and the migration is
confirmed live — `casey_notifications.reason` column exists on the
local DB).

## Part B: Analysis engine

### `integrations/anthropic_client.py` (Step 1) — forced tool use is not available

The task asked for forced tool use (`tool_choice` pinned to one tool) so
the output is structured. **Checked directly against Anthropic's own
current API documentation**: forced `tool_choice` (`type: "tool"` or
`"any"`) returns a 400 on every current-generation model, **including
`claude-sonnet-5-5`** (`casey_model`'s default). The documented
workaround — used here — is `tool_choice: {"type": "auto"}` plus
`strict: true` on the tool (requiring `additionalProperties: false` in
`input_schema`, which keeps the same schema-valid-output guarantee)
plus an explicit system-prompt instruction to always call the tool.
Also set `output_config: {"effort": "low"}`: this is a single-message
classification-shaped task, which is the documented case for low
effort (cheaper, and this model's thinking can't be disabled entirely).
Config: `casey_model` (default `claude-sonnet-5-5`), `casey_max_tokens`
(default `1024`), `casey_analysis_enabled` (default `false`).

### `agents/casey/analyze.py` (Step 2 + Part C Step 3's CLI)

Prompt version `"casey-analysis-v1"` (stored in `message_analyses.
prompt_version`). Input to the model: the target message (wrapped in
`<customer_message>`/`</customer_message>` delimiters, with the system
prompt stating it is data, not instructions); up to
`casey_history_messages` (default 10) previous messages for the same
`job_key`, last 60 days, oldest first, each with its stored sentiment/
summary (a history item can be marked `is_summary: True` for a
scenario-summary item like Sandstrom's pre-cancellation history — it's
rendered with an explicit `"[summary, not a verbatim message]"` marker
so the model never mistakes it for an actual quote); the job's current
open flag (severity/reason/summary) if any.

Output schema (all enums from `taxonomy.py`, `strict: true` +
`additionalProperties: false`): `sentiment`, `sentiment_score`,
`is_strong_praise`, `red_flag_suggested`, `severity` (nullable),
`primary_reason`, `additional_reasons[]`, `reason_notes`, `root_cause`,
`issue_summary` (≤240 chars), `evidence_quote`,
`same_complaint_as_open_flag`, `trajectory`
(`improving|stable|worsening|unknown`), `confidence`.

Validation (`validate_analysis_output()`): all enums checked against
`taxonomy.py`; `"Other (explain in Notes)"` requires `reason_notes`
(reuses `taxonomy.validate_reasons()`); `evidence_quote`, when
non-empty, must be an exact substring of the target message's own
text. On any validation failure, retries **once**; if the retry also
fails, the analysis is stored `status="failed"` with the raw model
output (`raw_json`) and **no flag is ever created** from it
(`suggested_reasons` forced to `[]`).

CLI: `python -m agents.casey.analyze --limit N [--only-matched]
[--dry-run]` (default `limit=10`, default `only_matched=True`: only
messages with a resolved `job_id` are ever analyzed). `--dry-run` makes
**no API call** — it estimates token cost with a rough ~4-chars/token
heuristic (no tokenizer dependency) and prints the count that would be
analyzed.

New `message_analyses` columns (migration verified on the local DB):
`suggested_primary_reason`, `reason_notes`, `same_complaint_as_open_flag`,
`trajectory`, `confidence`, `status` (`ok`|`failed`, default `ok`).

### `scripts/casey_eval.py` (Step 3)

Evaluation harness over the Stage 6 calibration golden set. Makes **no
database writes**. Groups cases by customer, sorts each customer's
cases chronologically (a missing date — the Sandstrom pre-cancellation
summary — sorts first, since it describes what came *before* the Oct
20 message), and feeds each customer's own prior results forward as
`history` for their later messages, so Julie Harrison's Sep 16/Sep
26/Oct 9 examples and Danielle Ewen's Oct 1/5/19 examples build on each
other exactly as a live run would. Prints a table (customer, date,
expected, got, pass/fail, confidence), the pass count, token usage, and
an estimated cost (Sonnet 5.5's current per-token price — $2/$10 per
1M — hardcoded as a documented snapshot, not fetched live).

Pass rules: the two "Low or Neutral boundary" examples pass on
`None`/no-flag or `Low`; Caroline Day passes on `Medium` or `High`;
every other example must match the expected severity exactly.

`--offline` uses one recorded response fixture
(`tests/fixtures/casey/eval_offline_response.json`) instead of a real
API call, so the harness (and its tests) run without network access —
its purpose is exercising the plumbing end-to-end, not a meaningful
pass rate (the fixture always returns the same neutral/no-flag
output). A real run (no `--offline`) requires `ANTHROPIC_API_KEY` to be
set; without it, `run_eval()` raises immediately rather than silently
running offline.
