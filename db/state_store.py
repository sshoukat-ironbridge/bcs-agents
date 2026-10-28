import os
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from utils.logger import get_logger

load_dotenv()
logger = get_logger("db.state_store")

# --- DB setup with graceful fallback ---

_db_available = False
_engine = None
_Session = None
Base = None

try:
    from sqlalchemy import (
        create_engine, Column, Integer, String, DateTime, Date,
        Boolean, UniqueConstraint, text, Text, ForeignKey, JSON, Float, Index,
        or_, and_,
    )
    from sqlalchemy.orm import declarative_base, sessionmaker

    _engine = create_engine(os.getenv("POSTGRES_URL"), pool_pre_ping=True)
    _Session = sessionmaker(bind=_engine)
    Base = declarative_base()
    _db_available = True
except Exception as e:
    logger.critical(f"PostgreSQL connection failed — running in-memory only: {e}")


# --- ORM models ---

if _db_available:
    class OcaFlag(Base):
        __tablename__ = "oca_flags"

        id = Column(Integer, primary_key=True)
        job_id = Column(String, nullable=False, index=True)
        flag_type = Column(String, nullable=False)
        details = Column(String, nullable=True)
        urgency = Column(String, nullable=True)
        first_flagged_at = Column(DateTime(timezone=True), nullable=False)
        last_alerted_at = Column(DateTime(timezone=True), nullable=True)
        resolved_at = Column(DateTime(timezone=True), nullable=True)
        alert_count = Column(Integer, default=0, nullable=False)

    class CaseyActiveJob(Base):
        __tablename__ = "casey_active_jobs"
        __table_args__ = (UniqueConstraint("client_name", "sheet_tab", "primary_job_type", name="uq_client_sheet_tab_jobtype"),)

        id = Column(Integer, primary_key=True)
        client_name = Column(String, nullable=False)
        pm_name = Column(String, nullable=True)
        job_type = Column(String, nullable=True)
        start_date = Column(String, nullable=True)
        deposit_date = Column(String, nullable=True)
        estimated_start_window = Column(String, nullable=True)
        assigned_crew_sub = Column(String, nullable=True)
        last_pm_contact = Column(Date, nullable=True)
        most_recent_contact = Column(String, nullable=True)
        pm_communication_history = Column(Text, nullable=True)
        customer_email = Column(String, nullable=True)
        customer_phone = Column(String, nullable=True)
        synced_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        # Extended fields synced from Google Sheet
        client_mood = Column(String, nullable=True)
        complaint_note = Column(String, nullable=True)
        job_description = Column(String, nullable=True)
        estimator_name = Column(String, nullable=True)
        to_collect = Column(String, nullable=True)
        total_project = Column(String, nullable=True)
        sheet_tab = Column(String, nullable=True)
        deadline_to_start = Column(String, nullable=True)
        address = Column(Text, nullable=True)
        comms_hold = Column(Boolean, nullable=False, default=False)
        comms_hold_reason = Column(Text, nullable=True)
        comms_hold_set_at = Column(DateTime(timezone=True), nullable=True)
        comms_hold_until = Column(Date, nullable=True)
        # Upsert key v2: allows multi-job clients to have one row per job type
        primary_job_type = Column(String, nullable=True)
        # Soft-delete: jobs removed from sheet are marked retired, not deleted
        retired    = Column(Boolean, nullable=False, default=False)
        retired_at = Column(DateTime(timezone=True), nullable=True)

    class AppConfig(Base):
        __tablename__ = "app_config"
        key   = Column(String, primary_key=True)
        value = Column(Text, nullable=True)

    class PmConfig(Base):
        __tablename__ = "pm_config"
        id             = Column(Integer, primary_key=True, autoincrement=True)
        name           = Column(String)
        email          = Column(String)
        slack_user_id  = Column(String)
        display_note   = Column(Text, nullable=True)
        dialpad_user_id = Column(String, nullable=True)

    class SalesRepConfig(Base):
        __tablename__ = "sales_rep_config"
        id               = Column(Integer, primary_key=True, autoincrement=True)
        name             = Column(String)
        email            = Column(String)
        hubspot_owner_id = Column(String)

    class AgentRun(Base):
        __tablename__ = "agent_runs"
        id          = Column(Integer, primary_key=True, autoincrement=True)
        agent       = Column(String)
        started_at  = Column(DateTime, default=datetime.utcnow)
        finished_at = Column(DateTime, nullable=True)
        status      = Column(String)          # running / success / error
        log         = Column(Text, nullable=True)
        summary     = Column(String, nullable=True)  # one-line summary, e.g. "10 emails sent, 2 escalations"

    class DashboardUser(Base):
        __tablename__ = "dashboard_users"
        id            = Column(Integer, primary_key=True, autoincrement=True)
        email         = Column(String, unique=True, nullable=False, index=True)
        password_hash = Column(String, nullable=False)
        # Role-based visibility (agents/casey/access.py). Existing rows default
        # to 'admin' via the ALTER TABLE default below, so every account that
        # could already log in keeps full access unchanged.
        role          = Column(String, nullable=False, default="pm")  # safety net only; create_user() always passes one
        linked_name   = Column(String, nullable=True)  # PM/estimator display name, for role in (pm, estimator)
        created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        updated_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class DashboardSession(Base):
        __tablename__ = "dashboard_sessions"
        token      = Column(String, primary_key=True)
        user_id    = Column(Integer, nullable=False, index=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        expires_at = Column(DateTime(timezone=True), nullable=False)

    # --- Casey foundations (read OCA's casey_active_jobs read-only) ---

    class CustomerMessage(Base):
        __tablename__ = "customer_messages"
        __table_args__ = (
            UniqueConstraint("source", "external_id", name="uq_customer_messages_source_external_id"),
            Index("ix_customer_messages_job_key", "job_key"),
            Index("ix_customer_messages_thread_id", "thread_id"),
            Index("ix_customer_messages_occurred_at", "occurred_at"),
        )
        id              = Column(Integer, primary_key=True, autoincrement=True)
        source          = Column(String, nullable=False)  # gmail | dialpad_call | dialpad_sms
        external_id     = Column(String, nullable=False)
        thread_id       = Column(String, nullable=True)
        job_id          = Column(Integer, ForeignKey("casey_active_jobs.id"), nullable=True)
        job_key         = Column(String, nullable=True)  # "client|tab|primary_job_type"
        customer_email  = Column(String, nullable=True)
        customer_phone  = Column(String, nullable=True)  # doubles as "contact_phone" for calls
        duration_ms     = Column(Integer, nullable=True)  # dialpad_call only
        call_state      = Column(String, nullable=True)  # dialpad_call only, e.g. "connected"
        direction       = Column(String, nullable=False)  # inbound | outbound | internal
        occurred_at     = Column(DateTime(timezone=True), nullable=False)
        sender          = Column(String, nullable=True)
        subject         = Column(Text, nullable=True)
        body_text       = Column(Text, nullable=True)
        is_automated    = Column(Boolean, nullable=False, default=False)
        pm_name         = Column(String, nullable=True)
        estimator_name  = Column(String, nullable=True)
        ingested_at     = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        raw_ref         = Column(Text, nullable=True)
        is_demo         = Column(Boolean, nullable=False, default=False)  # Stage 9: demo-mode seed data
        # Stage 10a: matched | ambiguous | unmatched, set from the match
        # result ingest already computes — previously thrown away (both
        # ambiguous and unmatched stored job_id=None with no way to tell
        # them apart after the fact; see docs/casey_foundation.md).
        match_status    = Column(String, nullable=True)
        # Why this row is never analyzed even though it's stored, e.g.
        # "no_customer_text" — reused by get_messages_to_analyze().
        skip_reason     = Column(String, nullable=True)

    class MessageAnalysis(Base):
        __tablename__ = "message_analyses"
        id                   = Column(Integer, primary_key=True, autoincrement=True)
        message_id           = Column(Integer, ForeignKey("customer_messages.id"), nullable=False, index=True)
        model                = Column(String, nullable=True)
        prompt_version       = Column(String, nullable=True)
        sentiment            = Column(String, nullable=True)  # positive | neutral | negative
        sentiment_score      = Column(Float, nullable=True)
        is_strong_praise     = Column(Boolean, nullable=False, default=False)
        red_flag_suggested   = Column(Boolean, nullable=True)
        suggested_severity   = Column(String, nullable=True)
        suggested_primary_reason = Column(String, nullable=True)
        suggested_reasons    = Column(JSON, nullable=True)  # additional_reasons list
        reason_notes         = Column(Text, nullable=True)
        suggested_root_cause = Column(String, nullable=True)
        summary              = Column(Text, nullable=True)
        evidence_quote       = Column(Text, nullable=True)
        same_complaint_as_open_flag = Column(Boolean, nullable=True)
        trajectory           = Column(String, nullable=True)  # improving | stable | worsening | unknown
        confidence           = Column(Float, nullable=True)
        status               = Column(String, nullable=False, default="ok")  # ok | failed
        raw_json             = Column(JSON, nullable=True)
        created_at           = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        is_demo              = Column(Boolean, nullable=False, default=False)  # Stage 9: demo-mode seed data

    class CaseyFlag(Base):
        __tablename__ = "casey_flags"
        __table_args__ = (
            Index("ix_casey_flags_job_key", "job_key"),
            Index("ix_casey_flags_status", "status"),
            Index("ix_casey_flags_severity", "severity"),
            # Partial unique index: only one non-closed flag per dedup_key —
            # an already-closed flag never blocks a fresh one for the same key.
            Index("uq_casey_flags_open_dedup_key", "dedup_key", unique=True,
                  postgresql_where=text("closed_at IS NULL")),
        )
        id                  = Column(Integer, primary_key=True, autoincrement=True)
        job_id              = Column(Integer, ForeignKey("casey_active_jobs.id"), nullable=True)
        job_key             = Column(String, nullable=True)
        customer_name       = Column(String, nullable=True)
        address             = Column(Text, nullable=True)
        pm_name             = Column(String, nullable=True)
        estimator_name      = Column(String, nullable=True)
        severity            = Column(String, nullable=False)
        status              = Column(String, nullable=False, default="Open")
        primary_reason      = Column(String, nullable=True)
        additional_reasons  = Column(JSON, nullable=True)
        root_cause          = Column(String, nullable=False, default="Undetermined (default)")
        reason_notes        = Column(Text, nullable=True)
        issue_summary       = Column(Text, nullable=True)
        follow_up_required  = Column(Boolean, nullable=False, default=True)
        dedup_key           = Column(String, nullable=False)
        first_message_id    = Column(Integer, ForeignKey("customer_messages.id"), nullable=True)
        latest_message_id   = Column(Integer, ForeignKey("customer_messages.id"), nullable=True)
        created_at          = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        updated_at          = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        closed_at           = Column(DateTime(timezone=True), nullable=True)
        resolution_reason   = Column(Text, nullable=True)
        resolved_by         = Column(String, nullable=True)
        notes               = Column(Text, nullable=True)
        is_demo             = Column(Boolean, nullable=False, default=False)  # Stage 9: demo-mode seed data

    class CaseyFlagEvent(Base):
        __tablename__ = "casey_flag_events"
        id         = Column(Integer, primary_key=True, autoincrement=True)
        # Nullable: a "no_op" analysis (no flag created or matched) still
        # gets an event so get_unprocessed_analyses() marks it processed.
        flag_id    = Column(Integer, ForeignKey("casey_flags.id"), nullable=True, index=True)
        event_type = Column(String, nullable=False)  # created | severity_escalated | severity_lowered_by_human |
                                                       # status_changed | improving_sentiment | note |
                                                       # reason_changed | notification_planned
        old_value  = Column(Text, nullable=True)
        new_value  = Column(Text, nullable=True)
        actor      = Column(String, nullable=False)  # "casey" or a user name
        message_id = Column(Integer, ForeignKey("customer_messages.id"), nullable=True)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        is_demo    = Column(Boolean, nullable=False, default=False)  # Stage 9: demo-mode seed data

    class CaseyNotification(Base):
        __tablename__ = "casey_notifications"
        # LIVE-CHECKED: plan_notifications() intentionally returns several
        # rows (Slack channel + DM + one per email recipient) sharing ONE
        # dedup_key per batch — a dedup_key-only unique constraint rejects
        # every row after the first with a UniqueViolation. Unique on
        # (dedup_key, channel, recipient) instead: the same channel+
        # recipient for the same unchanged dedup_key never double-inserts,
        # but a batch's other channels/recipients still can.
        __table_args__ = (
            UniqueConstraint("dedup_key", "channel", "recipient",
                              name="uq_casey_notifications_dedup_key_channel_recipient"),
        )
        id         = Column(Integer, primary_key=True, autoincrement=True)
        flag_id    = Column(Integer, ForeignKey("casey_flags.id"), nullable=True)
        watch_id   = Column(Integer, ForeignKey("casey_response_watches.id"), nullable=True)
        channel    = Column(String, nullable=False)  # slack_channel | slack_dm | email
        recipient  = Column(String, nullable=False)
        severity   = Column(String, nullable=True)
        dedup_key  = Column(String, nullable=False)
        status     = Column(String, nullable=False, default="planned")  # planned | sending | sent | suppressed | failed
        reason     = Column(String, nullable=True)  # e.g. "backfill" when status == "suppressed"
        # The exact message text built at planning time (agents/casey/
        # notification_plan.py's build_message()) — persisted rather than
        # rebuilt at send time, so an escalation's "ESCALATED to X from Y"
        # wording and the flag's severity/link snapshot at the moment this
        # row was planned never drift from what was actually decided.
        message    = Column(Text, nullable=True)
        attempts   = Column(Integer, nullable=False, default=0)
        planned_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        sent_at    = Column(DateTime(timezone=True), nullable=True)
        error      = Column(Text, nullable=True)

    class CaseyResponseWatch(Base):
        __tablename__ = "casey_response_watches"
        __table_args__ = (
            Index("ix_casey_response_watches_thread_id", "thread_id"),
            Index("ix_casey_response_watches_status", "status"),
        )
        id                   = Column(Integer, primary_key=True, autoincrement=True)
        thread_id            = Column(String, nullable=True)
        inbound_message_id   = Column(Integer, ForeignKey("customer_messages.id"), nullable=False)
        job_key              = Column(String, nullable=True)
        pm_name              = Column(String, nullable=True)
        customer_email       = Column(String, nullable=True)
        received_at          = Column(DateTime(timezone=True), nullable=False)
        due_at               = Column(DateTime(timezone=True), nullable=False)
        status               = Column(String, nullable=False, default="waiting")  # waiting|responded|overdue|excluded
        responded_at         = Column(DateTime(timezone=True), nullable=True)
        response_message_id  = Column(Integer, ForeignKey("customer_messages.id"), nullable=True)
        alert_notification_id = Column(Integer, ForeignKey("casey_notifications.id"), nullable=True)
        is_demo              = Column(Boolean, nullable=False, default=False)  # Stage 9: demo-mode seed data

    class IngestCursor(Base):
        __tablename__ = "ingest_cursors"
        source       = Column(String, primary_key=True)
        cursor_value = Column(Text, nullable=True)
        updated_at   = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class IngestRunStats(Base):
        """One row per mailbox (Gmail) or PM (Dialpad) per ingest run —
        Stage 10a: the counters a run computes in memory (seen, skipped,
        matched/ambiguous/unmatched, ...) were previously discarded the
        moment the process exited (see docs/casey_foundation.md)."""
        __tablename__ = "ingest_run_stats"
        id              = Column(Integer, primary_key=True, autoincrement=True)
        source          = Column(String, nullable=False)  # gmail | dialpad_call
        mailbox_or_pm   = Column(String, nullable=True)
        started_at      = Column(DateTime(timezone=True), nullable=False)
        finished_at     = Column(DateTime(timezone=True), nullable=True)
        counters        = Column(JSON, nullable=True)

    class WebhookEvent(Base):
        __tablename__ = "webhook_events"
        id              = Column(Integer, primary_key=True, autoincrement=True)
        source          = Column(String, nullable=False)
        received_at     = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        signature_valid = Column(Boolean, nullable=False)
        payload         = Column(JSON, nullable=True)
        processed_at    = Column(DateTime(timezone=True), nullable=True)
        error           = Column(Text, nullable=True)


# --- In-memory fallback stores ---
_memory_store: dict[tuple[str, str], list[datetime]] = {}
_memory_jobs: dict[tuple[str, str, str], dict] = {}  # keyed by (client_name, sheet_tab, primary_job_type)


def _row_to_dict(row: "CaseyActiveJob") -> dict:
    return {
        "id": row.id,
        "client_name": row.client_name,
        "pm_name": row.pm_name,
        "job_type": row.job_type,
        "start_date": row.start_date,
        "deposit_date": row.deposit_date,
        "estimated_start_window": row.estimated_start_window,
        "assigned_crew_sub": row.assigned_crew_sub,
        "last_pm_contact": row.last_pm_contact,
        "most_recent_contact": row.most_recent_contact,
        "pm_communication_history": row.pm_communication_history,
        "customer_email": row.customer_email,
        "customer_phone": row.customer_phone,
        "synced_at": row.synced_at,
        "client_mood": row.client_mood,
        "complaint_note": row.complaint_note,
        "job_description": row.job_description,
        "estimator_name": row.estimator_name,
        "to_collect": row.to_collect,
        "total_project": row.total_project,
        "sheet_tab": row.sheet_tab,
        "deadline_to_start": row.deadline_to_start,
        "address": row.address,
        "comms_hold": row.comms_hold or False,
        "comms_hold_reason": row.comms_hold_reason,
        "comms_hold_set_at": row.comms_hold_set_at,
        "comms_hold_until": row.comms_hold_until,
        "primary_job_type": row.primary_job_type or "",
        "retired":    row.retired or False,
        "retired_at": row.retired_at,
    }


# --- Table setup ---

def init_db() -> None:
    if not _db_available:
        logger.warning("init_db: DB not available, skipping")
        return
    try:
        Base.metadata.create_all(_engine)
        with _engine.connect() as conn:
            conn.execute(text("ALTER TABLE oca_flags ADD COLUMN IF NOT EXISTS details TEXT"))
            conn.execute(text("ALTER TABLE oca_flags ADD COLUMN IF NOT EXISTS urgency TEXT"))
            # Extended job fields
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS client_mood TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS complaint_note TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS job_description TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS estimator_name TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS to_collect TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS total_project TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS sheet_tab VARCHAR"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS pm_communication_history TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS deadline_to_start VARCHAR"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS address TEXT"))
            conn.execute(text("ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS summary VARCHAR"))
            # Fix 3: per-customer hold flag
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS comms_hold BOOLEAN NOT NULL DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS comms_hold_reason TEXT"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS comms_hold_set_at TIMESTAMP WITH TIME ZONE"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS comms_hold_until DATE"))
            # Fix 3 (Fernanda display_note)
            conn.execute(text("ALTER TABLE pm_config ADD COLUMN IF NOT EXISTS display_note TEXT"))
            # Upsert key v2: per-job-type tracking for multi-job clients
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS primary_job_type VARCHAR DEFAULT ''"))
            conn.commit()

            # Change 4: one-time orphan cleanup — per (client_name, sheet_tab) keep only the most
            # recent row; run BEFORE the constraint migration so there are no duplicates to block it.
            if not conn.execute(
                text("SELECT value FROM app_config WHERE key = 'orphan_cleanup_done'")
            ).fetchone():
                try:
                    conn.execute(text("""
                        DELETE FROM casey_active_jobs
                        WHERE id NOT IN (
                            SELECT DISTINCT ON (client_name, COALESCE(sheet_tab,''))
                                id
                            FROM casey_active_jobs
                            ORDER BY client_name, COALESCE(sheet_tab,''),
                                     synced_at DESC NULLS LAST, id DESC
                        )
                    """))
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES ('orphan_cleanup_done', 'true')")
                    )
                    conn.commit()
                    logger.info("One-time orphan cleanup: removed duplicate (client_name, sheet_tab) rows")
                except Exception as _e:
                    conn.rollback()
                    logger.warning(f"orphan_cleanup migration: {_e}")

            # Change 4: migrate UNIQUE constraint from (client_name, pm_name) to (client_name, sheet_tab).
            if not conn.execute(
                text("SELECT value FROM app_config WHERE key = 'upsert_key_migrated'")
            ).fetchone():
                try:
                    conn.execute(text(
                        "ALTER TABLE casey_active_jobs DROP CONSTRAINT IF EXISTS uq_client_pm"
                    ))
                    conn.execute(text(
                        "ALTER TABLE casey_active_jobs "
                        "ADD CONSTRAINT uq_client_sheet_tab UNIQUE (client_name, sheet_tab)"
                    ))
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES ('upsert_key_migrated', 'true')")
                    )
                    conn.commit()
                    logger.info("Migrated UNIQUE constraint: (client_name, pm_name) → (client_name, sheet_tab)")
                except Exception as _e:
                    conn.rollback()
                    logger.warning(f"upsert_key_migrated migration: {_e}")

            # Change 4 v2: migrate UNIQUE constraint to include primary_job_type so
            # multi-job clients (e.g. Peter Speros) can have one row per job type.
            if not conn.execute(
                text("SELECT value FROM app_config WHERE key = 'upsert_key_v2_migrated'")
            ).fetchone():
                try:
                    conn.execute(text(
                        "UPDATE casey_active_jobs SET primary_job_type = '' WHERE primary_job_type IS NULL"
                    ))
                    conn.execute(text("""
                        DELETE FROM casey_active_jobs
                        WHERE id NOT IN (
                            SELECT DISTINCT ON (client_name, COALESCE(sheet_tab,''), COALESCE(primary_job_type,''))
                                id
                            FROM casey_active_jobs
                            ORDER BY client_name, COALESCE(sheet_tab,''), COALESCE(primary_job_type,''),
                                     synced_at DESC NULLS LAST, id DESC
                        )
                    """))
                    conn.execute(text(
                        "ALTER TABLE casey_active_jobs DROP CONSTRAINT IF EXISTS uq_client_sheet_tab"
                    ))
                    conn.execute(text("""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_constraint
                                WHERE conname = 'uq_client_sheet_tab_jobtype'
                                AND conrelid = 'casey_active_jobs'::regclass
                            ) THEN
                                ALTER TABLE casey_active_jobs ADD CONSTRAINT uq_client_sheet_tab_jobtype
                                UNIQUE (client_name, sheet_tab, primary_job_type);
                            END IF;
                        END $$
                    """))
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES ('upsert_key_v2_migrated', 'true')")
                    )
                    conn.commit()
                    logger.info("Migrated UNIQUE constraint to (client_name, sheet_tab, primary_job_type)")
                except Exception as _e:
                    conn.rollback()
                    logger.warning(f"upsert_key_v2_migrated migration: {_e}")

            # Soft-delete columns for sheet reconciliation
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS retired BOOLEAN NOT NULL DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE casey_active_jobs ADD COLUMN IF NOT EXISTS retired_at TIMESTAMP WITH TIME ZONE"))
            conn.commit()

            # Dialpad user ID per PM
            conn.execute(text("ALTER TABLE pm_config ADD COLUMN IF NOT EXISTS dialpad_user_id TEXT"))
            conn.commit()

            # Casey foundations: role-based dashboard access. Existing
            # accounts are backfilled to 'admin' ONCE (a row that already
            # existed before this migration keeps full access, unchanged).
            # Going forward the column default is 'pm' (least privileged) —
            # create_user() below still always requires an explicit role;
            # this default is only a safety net for a raw insert that
            # forgets the column.
            conn.execute(text("ALTER TABLE dashboard_users ADD COLUMN IF NOT EXISTS role TEXT"))
            conn.execute(text("UPDATE dashboard_users SET role = 'admin' WHERE role IS NULL"))
            conn.execute(text("ALTER TABLE dashboard_users ALTER COLUMN role SET DEFAULT 'pm'"))
            conn.execute(text("ALTER TABLE dashboard_users ALTER COLUMN role SET NOT NULL"))
            conn.execute(text("ALTER TABLE dashboard_users ADD COLUMN IF NOT EXISTS linked_name TEXT"))
            conn.commit()

            # Stage 5: Dialpad call ingestion fields on customer_messages.
            # customer_phone (already present) doubles as "contact_phone".
            conn.execute(text("ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS duration_ms INTEGER"))
            conn.execute(text("ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS call_state TEXT"))
            conn.execute(text("ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS customer_phone TEXT"))
            conn.commit()

            # Stage 6: casey_notifications' unique constraint was dedup_key
            # alone, which rejects every row after the first in a batch
            # that intentionally shares one dedup_key (Slack channel + DM +
            # email recipients) — widen it to (dedup_key, channel, recipient).
            conn.execute(text(
                "ALTER TABLE casey_notifications DROP CONSTRAINT IF EXISTS uq_casey_notifications_dedup_key"
            ))
            conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'uq_casey_notifications_dedup_key_channel_recipient'
                        AND conrelid = 'casey_notifications'::regclass
                    ) THEN
                        ALTER TABLE casey_notifications ADD CONSTRAINT
                        uq_casey_notifications_dedup_key_channel_recipient
                        UNIQUE (dedup_key, channel, recipient);
                    END IF;
                END $$
            """))
            conn.commit()

            # Stage 7: backfill suppression reason.
            conn.execute(text("ALTER TABLE casey_notifications ADD COLUMN IF NOT EXISTS reason TEXT"))
            conn.commit()

            # Stage 7: analysis engine output fields on message_analyses.
            conn.execute(text("ALTER TABLE message_analyses ADD COLUMN IF NOT EXISTS suggested_primary_reason TEXT"))
            conn.execute(text("ALTER TABLE message_analyses ADD COLUMN IF NOT EXISTS reason_notes TEXT"))
            conn.execute(text("ALTER TABLE message_analyses ADD COLUMN IF NOT EXISTS same_complaint_as_open_flag BOOLEAN"))
            conn.execute(text("ALTER TABLE message_analyses ADD COLUMN IF NOT EXISTS trajectory TEXT"))
            conn.execute(text("ALTER TABLE message_analyses ADD COLUMN IF NOT EXISTS confidence REAL"))
            conn.execute(text("ALTER TABLE message_analyses ADD COLUMN IF NOT EXISTS status TEXT"))
            conn.execute(text("UPDATE message_analyses SET status = 'ok' WHERE status IS NULL"))
            conn.execute(text("ALTER TABLE message_analyses ALTER COLUMN status SET DEFAULT 'ok'"))
            conn.execute(text("ALTER TABLE message_analyses ALTER COLUMN status SET NOT NULL"))
            conn.commit()

            # Stage 7: a "no_op" analysis event has no flag to attach to.
            conn.execute(text("ALTER TABLE casey_flag_events ALTER COLUMN flag_id DROP NOT NULL"))
            conn.commit()

            # Stage 8: persist the exact message text and send-attempt count.
            conn.execute(text("ALTER TABLE casey_notifications ADD COLUMN IF NOT EXISTS message TEXT"))
            conn.execute(text("ALTER TABLE casey_notifications ADD COLUMN IF NOT EXISTS attempts INTEGER"))
            conn.execute(text("UPDATE casey_notifications SET attempts = 0 WHERE attempts IS NULL"))
            conn.execute(text("ALTER TABLE casey_notifications ALTER COLUMN attempts SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE casey_notifications ALTER COLUMN attempts SET NOT NULL"))
            conn.commit()

            # Stage 9: demo-mode flag on the tables the Casey dashboard reads.
            for _demo_table in ("casey_flags", "casey_flag_events", "customer_messages",
                                 "message_analyses", "casey_response_watches"):
                conn.execute(text(f"ALTER TABLE {_demo_table} ADD COLUMN IF NOT EXISTS is_demo BOOLEAN"))
                conn.execute(text(f"UPDATE {_demo_table} SET is_demo = FALSE WHERE is_demo IS NULL"))
                conn.execute(text(f"ALTER TABLE {_demo_table} ALTER COLUMN is_demo SET DEFAULT FALSE"))
                conn.execute(text(f"ALTER TABLE {_demo_table} ALTER COLUMN is_demo SET NOT NULL"))
            conn.commit()

            # Stage 10a: match_status / skip_reason on customer_messages.
            conn.execute(text("ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS match_status TEXT"))
            conn.execute(text("ALTER TABLE customer_messages ADD COLUMN IF NOT EXISTS skip_reason TEXT"))
            conn.commit()

            # One-time migration: retire Patricia Anderson (manually removed from sheet)
            if not conn.execute(
                text("SELECT value FROM app_config WHERE key = 'patricia_anderson_retired'")
            ).fetchone():
                try:
                    conn.execute(text(
                        "UPDATE casey_active_jobs SET retired = true, retired_at = NOW() "
                        "WHERE client_name = 'Patricia Anderson' AND (retired = false OR retired IS NULL)"
                    ))
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES ('patricia_anderson_retired', 'true')")
                    )
                    conn.commit()
                    logger.info("One-time migration: retired Patricia Anderson from active jobs")
                except Exception as _e:
                    conn.rollback()
                    logger.warning(f"patricia_anderson_retired migration: {_e}")

            # Seed default app config if not already set
            _config_defaults = {
                # Notification routing — all agent alerts go to this Slack channel
                "slack_urgentmatters_channel":  "urgentmatters",
                # Chris notification email — receives pre-send and step-in alerts
                "chris_notification_email":     "chris.s@bluecollarscholars.net",
                # Sender address used when emailing Chris (must be a Google Workspace
                # account covered by the service account's domain-wide delegation)
                "notification_sender_email":    "",
                # Silent mode — when "true" all Slack messages and Chris notification
                # emails are suppressed (agents still run and log). Default ON so
                # nothing goes out on first deploy after a rebuild.
                "silent_mode":               "true",
                "silent_mode_toggled_at":    "",
                # Dialpad integration
                "dialpad_api_key":           "",
                "dialpad_api_base_url":      "https://dialpad.com/api/v2",
                # Casey notification recipients (agents/casey/notification_plan.py).
                # Empty by default — never hardcode a real address here.
                "casey_email_ana_maria":     "",
                "casey_email_tatiana":       "",
                "casey_email_chris":         "",
                # When set, replaces every Casey email recipient (local testing).
                "casey_email_override":      "",
                # Dialpad webhook signature verification (agents/casey, Step 9).
                "dialpad_webhook_secret":    "",
                # 24-consecutive-hour response window (agents/casey/response_watch.py).
                "casey_response_window_hours": "24",
                # SMS replies count as an answered customer email by default: off.
                "casey_sms_counts_as_response": "false",
                # Per-IP rate limit for unauthenticated webhook endpoints.
                "casey_webhook_rate_limit_per_minute": "30",
                # Dialpad call ingestion (agents/casey/ingest_dialpad.py).
                # Off by default - a scheduled run checks this; an explicit
                # CLI invocation (including --dry-run) always proceeds.
                "casey_dialpad_enabled": "false",
                # Transcripts are typically ready within ~46 minutes; wait
                # 60 before fetching so most calls aren't re-fetched empty.
                "casey_dialpad_transcript_lag_minutes": "60",
                # Re-fetch this much of the previous window on each run, so
                # a call near the edge (or a transcript that wasn't ready
                # yet) is never missed - upsert on (source, external_id)
                # makes the overlap idempotent.
                "casey_dialpad_overlap_hours": "2",
                # Gmail ingestion (agents/casey/ingest_gmail.py). No static
                # default for casey_gmail_mailboxes here on purpose: it
                # resolves dynamically to [notification_sender_email] at
                # call time if unset, so it can never drift from whatever
                # that key currently holds.
                "casey_gmail_max_messages": "500",
                "casey_gmail_overlap_hours": "2",
                "casey_gmail_store_unmatched": "false",
                # Off by default - agents/casey/main.py's scheduled-run
                # orchestration checks this; a direct CLI invocation of
                # ingest_gmail.py always runs regardless.
                "casey_gmail_enabled": "false",
                # Backfill suppression (Stage 7): a UTC ISO timestamp. Any
                # notification for an event before this timestamp, OR any
                # time this is unset, is stored suppressed/backfill instead
                # of planned. Unset until explicitly set at go-live — see
                # docs/casey_foundation.md for how to set it.
                "casey_live_since": "",
                # Analysis engine (integrations/anthropic_client.py,
                # agents/casey/analyze.py). Off by default - a scheduled
                # run checks this; an explicit CLI invocation always runs.
                "casey_analysis_enabled": "false",
                "casey_model": "claude-sonnet-5-5",
                "casey_max_tokens": "1024",
                # How much message history to feed the model per analysis.
                "casey_history_messages": "10",
                # Notification sender (Stage 8, agents/casey/notify.py). Off
                # by default — a scheduled run checks this; an explicit CLI
                # invocation of notify.py always runs regardless.
                "casey_notify_enabled": "false",
                # Base URL used to build a flag's dashboard link
                # (<base>/casey/flags/<id>). Empty by default — no link is
                # included until this is set.
                "casey_dashboard_base_url": "",
                # Max retries for a failed notification send — a failed
                # row goes back to "planned" and is retried on the next
                # scheduled agents/casey/main.py run (the run interval
                # itself is the backoff) until this many attempts.
                "casey_notify_max_retries": "3",
                # Local-safe send test channel (agents/casey/notify.py,
                # scripts/casey_send_test.py). Real Slack channel/DM
                # recipients are only used when CASEY_ENV=production;
                # otherwise every Slack send goes to this test channel.
                "casey_slack_test_channel": "",
                # Stage 9: demo mode for the Casey dashboard — on by default
                # locally so a fresh checkout shows seeded demo data, never
                # real customer text, until explicitly turned off.
                "casey_demo_mode": "true",
                # Stage 10a: mailboxes come from pm_config emails by
                # default; "manual" keeps using casey_gmail_mailboxes.
                "casey_gmail_mailboxes_mode": "pm_config",
                "casey_gmail_address_batch_size": "15",
                # A Dialpad call matching no active job is never stored
                # locally — mirrors casey_gmail_store_unmatched.
                "casey_dialpad_store_unmatched": "false",
                "casey_analysis_max_cost_usd": "15",
            }
            for key, default_val in _config_defaults.items():
                existing = conn.execute(
                    text("SELECT value FROM app_config WHERE key = :k"), {"k": key}
                ).fetchone()
                if not existing:
                    conn.execute(
                        text("INSERT INTO app_config (key, value) VALUES (:k, :v)"),
                        {"k": key, "v": default_val},
                    )
            conn.commit()

            # Fix 3: ensure "Fernanda" PM entry exists (Laura Arbelaez goes by Fernanda)
            fernanda_exists = conn.execute(
                text("SELECT id FROM pm_config WHERE email = 'laura.a@bluecollarscholars.net'")
            ).fetchone()
            if not fernanda_exists:
                conn.execute(
                    text(
                        "INSERT INTO pm_config (name, email, slack_user_id, display_note) "
                        "VALUES ('Fernanda', 'laura.a@bluecollarscholars.net', '', 'Laura Arbelaez')"
                    )
                )
                conn.commit()
                logger.info("Seeded Fernanda (Laura Arbelaez) PM entry")
        logger.info("Database tables verified/created")
    except Exception as e:
        logger.error(f"init_db error: {e}")


try:
    if _db_available:
        init_db()
except Exception as e:
    logger.error(f"Failed to initialise tables on import: {e}")


# --- AppConfig helpers ---

def get_config(key: str) -> "str | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            row = s.query(AppConfig).filter_by(key=key).first()
            return row.value if row else None
    except Exception as e:
        logger.error(f"get_config({key}): {e}")
        return None


def set_config(key: str, value: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(AppConfig).filter_by(key=key).first()
            if row:
                row.value = value
            else:
                s.add(AppConfig(key=key, value=value))
            s.commit()
    except Exception as e:
        logger.error(f"set_config({key}): {e}")


def casey_demo_mode() -> bool:
    """True unless casey_demo_mode is explicitly set to "false" — on by
    default so a fresh checkout never shows real customer text until
    someone deliberately turns demo mode off."""
    return (get_config("casey_demo_mode") or "true").strip().lower() != "false"


def get_all_config() -> dict:
    if not _db_available:
        return {}
    try:
        with _Session() as s:
            return {r.key: r.value for r in s.query(AppConfig).all()}
    except Exception as e:
        logger.error(f"get_all_config: {e}")
        return {}


# --- PM config helpers ---

def get_pm_list() -> list:
    if not _db_available:
        return []
    try:
        with _Session() as s:
            return [
                {
                    "id": r.id,
                    "full_name": r.name,
                    "email": r.email,
                    "slack_user_id": r.slack_user_id,
                    "display_note": r.display_note or "",
                    "dialpad_user_id": r.dialpad_user_id or "",
                }
                for r in s.query(PmConfig).order_by(PmConfig.id).all()
            ]
    except Exception as e:
        logger.error(f"get_pm_list: {e}")
        return []


# Maps known legacy sheet name variants (lowercase) to the pm_config.name they should resolve to.
# Add a new entry here when a PM is referenced by a new alias or full name in the Google Sheet.
_PM_NAME_ALIASES: dict[str, str] = {
    "andrea ortega":        "Andrea",
    "antonio cavaliere":    "Antonio",
    "carlos matus":         "Carlos",
    "daniel gomez":         "Daniel",
    "daniel gomez cortez":  "Daniel",
    "esteban estarita":     "Esteban",
    "laura arbelaez":       "Fernanda",   # pm_config.name for Laura Arbelaez is "Fernanda"
    "julie martinez":       "Julie",
    "jorge armella":        "Jorge",
    "santiago arango":      "Santiago",
    "laura pena":           "Laura",
    "laura peña":           "Laura",
    "gustavo zuluaga":      "Gustavo",
}


def find_pm(pm_name: str, pm_list: "list | None" = None) -> "dict | None":
    """Return the pm_config row matching pm_name, or None.

    Matching is case-insensitive and recognises both the pm_config.name (short form,
    e.g. 'Andrea') and any alias in _PM_NAME_ALIASES (e.g. 'Andrea Ortega',
    'ANDREA ORTEGA', 'andrea ortega').  Pass pm_list to avoid a DB round-trip
    inside a loop.  To add a new alias, edit _PM_NAME_ALIASES — one place, done.
    """
    if not pm_name or not pm_name.strip():
        return None
    if pm_list is None:
        pm_list = get_pm_list()
    key = pm_name.strip().lower()
    canonical = _PM_NAME_ALIASES.get(key)
    target = (canonical or pm_name.strip()).lower()
    for pm in pm_list:
        if pm.get("full_name", "").strip().lower() == target:
            return pm
    return None


def add_pm(name: str, email: str, slack_user_id: str, display_note: str = "", dialpad_user_id: str = "") -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(PmConfig(
                name=name,
                email=email,
                slack_user_id=slack_user_id,
                display_note=display_note or None,
                dialpad_user_id=dialpad_user_id.strip() or None,
            ))
            s.commit()
    except Exception as e:
        logger.error(f"add_pm: {e}")


def set_pm_dialpad_user_id(pm_id: int, dialpad_user_id: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(PmConfig).filter_by(id=pm_id).first()
            if row:
                row.dialpad_user_id = dialpad_user_id.strip() or None
                s.commit()
    except Exception as e:
        logger.error(f"set_pm_dialpad_user_id({pm_id}): {e}")


def delete_pm(pm_id: int) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(PmConfig).filter_by(id=pm_id).first()
            if row:
                s.delete(row)
                s.commit()
    except Exception as e:
        logger.error(f"delete_pm({pm_id}): {e}")


# --- Sales rep helpers ---

def get_sales_rep_list() -> list:
    if not _db_available:
        return []
    try:
        with _Session() as s:
            return [
                {"id": r.id, "name": r.name, "email": r.email, "hubspot_owner_id": r.hubspot_owner_id}
                for r in s.query(SalesRepConfig).order_by(SalesRepConfig.id).all()
            ]
    except Exception as e:
        logger.error(f"get_sales_rep_list: {e}")
        return []


def add_sales_rep(name: str, email: str, hubspot_owner_id: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(SalesRepConfig(name=name, email=email, hubspot_owner_id=hubspot_owner_id))
            s.commit()
    except Exception as e:
        logger.error(f"add_sales_rep: {e}")


def delete_sales_rep(rep_id: int) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(SalesRepConfig).filter_by(id=rep_id).first()
            if row:
                s.delete(row)
                s.commit()
    except Exception as e:
        logger.error(f"delete_sales_rep({rep_id}): {e}")


# --- AgentRun helpers ---

def create_agent_run(agent: str) -> int:
    if not _db_available:
        return -1
    try:
        with _Session() as s:
            run = AgentRun(agent=agent, started_at=datetime.utcnow(), status="running", log="")
            s.add(run)
            s.commit()
            s.refresh(run)
            return run.id
    except Exception as e:
        logger.error(f"create_agent_run: {e}")
        return -1


def append_agent_run_log(run_id: int, lines: str) -> None:
    if not _db_available or run_id < 0:
        return
    try:
        with _Session() as s:
            s.execute(
                text("UPDATE agent_runs SET log = COALESCE(log,'') || :lines WHERE id = :id"),
                {"lines": lines, "id": run_id},
            )
            s.commit()
    except Exception as e:
        logger.error(f"append_agent_run_log: {e}")


def finish_agent_run(run_id: int, status: str, summary: str = "") -> None:
    if not _db_available or run_id < 0:
        return
    try:
        with _Session() as s:
            run = s.query(AgentRun).filter_by(id=run_id).first()
            if run:
                run.status = status
                run.finished_at = datetime.utcnow()
                if summary:
                    run.summary = summary
                s.commit()
    except Exception as e:
        logger.error(f"finish_agent_run: {e}")


def set_agent_run_summary(run_id: int, summary: str) -> None:
    """Set the one-line summary on a run without touching status/finished_at.

    Used when a run's lifecycle (status/finished_at) is owned by something
    else — e.g. the dashboard's subprocess wrapper — but the run itself still
    wants to record what it did.
    """
    if not _db_available or run_id < 0:
        return
    try:
        with _Session() as s:
            run = s.query(AgentRun).filter_by(id=run_id).first()
            if run:
                run.summary = summary
                s.commit()
    except Exception as e:
        logger.error(f"set_agent_run_summary: {e}")


def get_agent_run(run_id: int) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(AgentRun).filter_by(id=run_id).first()
            if not r:
                return None
            return {
                "id": r.id, "agent": r.agent, "status": r.status,
                "log": r.log or "", "started_at": r.started_at, "finished_at": r.finished_at,
                "summary": r.summary or "",
            }
    except Exception as e:
        logger.error(f"get_agent_run: {e}")
        return None


def get_last_agent_run(agent: str) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(AgentRun).filter_by(agent=agent).order_by(AgentRun.id.desc()).first()
            if not r:
                return None
            return {
                "id": r.id, "agent": r.agent, "status": r.status,
                "log": r.log or "", "started_at": r.started_at, "finished_at": r.finished_at,
                "summary": r.summary or "",
            }
    except Exception as e:
        logger.error(f"get_last_agent_run: {e}")
        return None


def get_run_logs(agent: "str | None" = None, limit: int = 50) -> list[dict]:
    """Return the most recent agent runs (any/both agents), newest first."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            q = s.query(AgentRun)
            if agent:
                q = q.filter_by(agent=agent)
            rows = q.order_by(AgentRun.id.desc()).limit(limit).all()
            return [
                {
                    "id": r.id, "agent": r.agent, "status": r.status,
                    "log": r.log or "", "started_at": r.started_at, "finished_at": r.finished_at,
                    "summary": r.summary or "",
                }
                for r in rows
            ]
    except Exception as e:
        logger.error(f"get_run_logs: {e}")
        return []


# --- Dashboard auth (users + sessions) ---

def get_user_by_email(email: str) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(DashboardUser).filter_by(email=email.strip().lower()).first()
            if not r:
                return None
            return {"id": r.id, "email": r.email, "password_hash": r.password_hash,
                     "role": r.role or "pm", "linked_name": r.linked_name}
    except Exception as e:
        logger.error(f"get_user_by_email: {e}")
        return None


def get_user_by_id(user_id: int) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(DashboardUser).filter_by(id=user_id).first()
            if not r:
                return None
            return {"id": r.id, "email": r.email, "password_hash": r.password_hash,
                     "role": r.role or "pm", "linked_name": r.linked_name}
    except Exception as e:
        logger.error(f"get_user_by_id: {e}")
        return None


def create_user(email: str, password_hash: str, role: str, linked_name: str = "") -> "dict | None":
    """Creates a new dashboard user. role is REQUIRED — there is no
    implicit default here, by design (see docs/casey_foundation.md):
    agents.casey.access.DEFAULT_NEW_USER_ROLE ("pm", least privileged) is
    the safe value any future creation form should pre-fill, but the
    caller always decides and passes it explicitly. Raises ValueError for
    an unrecognized role."""
    from agents.casey.access import FULL_VISIBILITY_ROLES, RESTRICTED_ROLES
    valid_roles = FULL_VISIBILITY_ROLES | RESTRICTED_ROLES
    if role not in valid_roles:
        raise ValueError(f"role {role!r} is not one of {sorted(valid_roles)}")

    if not _db_available:
        return None
    try:
        with _Session() as s:
            user = DashboardUser(
                email=email.strip().lower(),
                password_hash=password_hash,
                role=role,
                linked_name=linked_name or None,
            )
            s.add(user)
            s.commit()
            return {"id": user.id, "email": user.email, "role": user.role, "linked_name": user.linked_name}
    except Exception as e:
        logger.error(f"create_user({email}): {e}")
        return None


def delete_user(email: str) -> bool:
    """Deletes a dashboard user by email. Used by scripts/seed_casey_demo_
    users.py --reset; never exposed as a UI action."""
    if not _db_available:
        return False
    try:
        with _Session() as s:
            row = s.query(DashboardUser).filter_by(email=email.strip().lower()).first()
            if not row:
                return False
            s.delete(row)
            s.commit()
            return True
    except Exception as e:
        logger.error(f"delete_user({email}): {e}")
        return False


def update_user_password(email: str, password_hash: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as s:
            r = s.query(DashboardUser).filter_by(email=email.strip().lower()).first()
            if not r:
                return False
            r.password_hash = password_hash
            r.updated_at = datetime.now(timezone.utc)
            s.commit()
            return True
    except Exception as e:
        logger.error(f"update_user_password: {e}")
        return False


def create_session(user_id: int, token: str, expires_at: datetime) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            s.add(DashboardSession(token=token, user_id=user_id, expires_at=expires_at))
            s.commit()
    except Exception as e:
        logger.error(f"create_session: {e}")


def get_session(token: str) -> "dict | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            r = s.query(DashboardSession).filter_by(token=token).first()
            if not r:
                return None
            return {"token": r.token, "user_id": r.user_id, "expires_at": r.expires_at}
    except Exception as e:
        logger.error(f"get_session: {e}")
        return None


def delete_session(token: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            r = s.query(DashboardSession).filter_by(token=token).first()
            if r:
                s.delete(r)
                s.commit()
    except Exception as e:
        logger.error(f"delete_session: {e}")


# --- Casey active jobs ---

def upsert_active_job(job: dict) -> None:
    client_name = job["client_name"]
    pm_name = job.get("pm_name") or ""
    sheet_tab = job.get("sheet_tab") or ""
    primary_job_type = job.get("primary_job_type") or ""
    now = datetime.now(timezone.utc)

    if not _db_available:
        key = (client_name, sheet_tab, primary_job_type)
        existing = _memory_jobs.get(key)
        if existing:
            for field in ("job_type", "start_date", "deposit_date", "estimated_start_window",
                          "assigned_crew_sub", "last_pm_contact", "most_recent_contact",
                          "pm_communication_history",
                          "customer_email", "customer_phone", "client_mood", "complaint_note",
                          "job_description", "estimator_name", "to_collect", "total_project",
                          "sheet_tab", "deadline_to_start", "pm_name", "address"):
                existing[field] = job.get(field)
            if "comms_hold" in job:
                existing["comms_hold"] = job["comms_hold"]
                existing["comms_hold_reason"] = job.get("comms_hold_reason")
            existing["synced_at"] = now
        else:
            _memory_jobs[key] = {**job, "synced_at": now}
        return

    try:
        with _Session() as session:
            # Look up by (client_name, sheet_tab, primary_job_type) so multi-job clients
            # can each have their own row, and PM name changes still update in place.
            row = (
                session.query(CaseyActiveJob)
                .filter_by(client_name=client_name, sheet_tab=sheet_tab, primary_job_type=primary_job_type)
                .first()
            )
            if row:
                # Un-retire if the job has returned to the sheet
                if row.retired:
                    row.retired = False
                    row.retired_at = None
                    logger.info(f"UN-RETIRED {client_name} — job returned to sheet")
                row.pm_name = pm_name
                row.primary_job_type = primary_job_type
                row.job_type = job.get("job_type")
                row.start_date = job.get("start_date")
                row.deposit_date = job.get("deposit_date")
                row.estimated_start_window = job.get("estimated_start_window")
                row.assigned_crew_sub = job.get("assigned_crew_sub")
                row.last_pm_contact = job.get("last_pm_contact")
                row.most_recent_contact = job.get("most_recent_contact")
                row.pm_communication_history = job.get("pm_communication_history")
                row.customer_email = job.get("customer_email")
                row.customer_phone = job.get("customer_phone")
                row.client_mood = job.get("client_mood")
                row.complaint_note = job.get("complaint_note")
                row.job_description = job.get("job_description")
                row.estimator_name = job.get("estimator_name")
                row.to_collect = job.get("to_collect")
                row.total_project = job.get("total_project")
                row.sheet_tab = job.get("sheet_tab")
                row.deadline_to_start = job.get("deadline_to_start")
                row.address = job.get("address")
                row.synced_at = now
                # Sheet-driven comms hold: written on every sync so hold clears
                # automatically when the PM cell returns to a real name.
                if "comms_hold" in job:
                    row.comms_hold = job["comms_hold"]
                    row.comms_hold_reason = job.get("comms_hold_reason")
                    if job["comms_hold"] and not row.comms_hold_set_at:
                        row.comms_hold_set_at = now
            else:
                _hold = job.get("comms_hold", False)
                session.add(CaseyActiveJob(
                    client_name=client_name,
                    pm_name=pm_name,
                    primary_job_type=primary_job_type,
                    job_type=job.get("job_type"),
                    start_date=job.get("start_date"),
                    deposit_date=job.get("deposit_date"),
                    estimated_start_window=job.get("estimated_start_window"),
                    assigned_crew_sub=job.get("assigned_crew_sub"),
                    last_pm_contact=job.get("last_pm_contact"),
                    most_recent_contact=job.get("most_recent_contact"),
                    pm_communication_history=job.get("pm_communication_history"),
                    customer_email=job.get("customer_email"),
                    customer_phone=job.get("customer_phone"),
                    client_mood=job.get("client_mood"),
                    complaint_note=job.get("complaint_note"),
                    job_description=job.get("job_description"),
                    estimator_name=job.get("estimator_name"),
                    to_collect=job.get("to_collect"),
                    total_project=job.get("total_project"),
                    sheet_tab=job.get("sheet_tab"),
                    deadline_to_start=job.get("deadline_to_start"),
                    address=job.get("address"),
                    comms_hold=_hold,
                    comms_hold_reason=job.get("comms_hold_reason"),
                    comms_hold_set_at=now if _hold else None,
                    synced_at=now,
                ))
            session.commit()
    except Exception as e:
        logger.error(f"DB error in upsert_active_job ({client_name}): {e}")


def get_all_active_jobs() -> list[dict]:
    if not _db_available:
        return [j for j in _memory_jobs.values() if not j.get("retired")]
    try:
        with _Session() as session:
            rows = (
                session.query(CaseyActiveJob)
                .filter(
                    (CaseyActiveJob.retired == False) | (CaseyActiveJob.retired == None)
                )
                .all()
            )
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"DB error in get_all_active_jobs: {e}")
        return []


def get_summary() -> dict[str, int]:
    """Row count on casey_active_jobs (unchanged semantics from before the
    cleanup: every row, retired included). The old escalation/due-for-update
    reminder concept was removed with legacy Casey; see
    docs/casey_foundation.md."""
    if not _db_available:
        return {"total": len(_memory_jobs)}

    try:
        with _Session() as session:
            return {"total": session.query(CaseyActiveJob).count()}
    except Exception as e:
        logger.error(f"DB error in get_summary: {e}")
        return {"total": 0}


# --- OCA flag functions ---

def flag_exists(job_id: str, flag_type: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as session:
            return (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            ) is not None
    except Exception as e:
        logger.error(f"DB error in flag_exists: {e}")
        return False


def create_flag(job_id: str, flag_type: str, details: str = "", urgency: str = "") -> None:
    if not _db_available:
        return
    now = datetime.now(timezone.utc)
    try:
        with _Session() as session:
            session.add(OcaFlag(
                job_id=job_id,
                flag_type=flag_type,
                details=details or None,
                urgency=urgency or None,
                first_flagged_at=now,
                last_alerted_at=now,
                alert_count=1,
            ))
            session.commit()
    except Exception as e:
        logger.error(f"DB error in create_flag ({job_id}/{flag_type}): {e}")


def should_alert_again(job_id: str, flag_type: str, cooldown_hours: int = 24) -> bool:
    if not _db_available:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag:
                return True
            if flag.last_alerted_at is None:
                return True
            return flag.last_alerted_at < cutoff
    except Exception as e:
        logger.error(f"DB error in should_alert_again: {e}")
        return False


def get_flag_alert_age_hours(job_id: str, flag_type: str) -> "float | None":
    """Hours since the active (unresolved) flag was last alerted, or None if no such flag/timestamp."""
    if not _db_available:
        return None
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag or flag.last_alerted_at is None:
                return None
            return (datetime.now(timezone.utc) - flag.last_alerted_at).total_seconds() / 3600
    except Exception as e:
        logger.error(f"DB error in get_flag_alert_age_hours: {e}")
        return None


def update_flag_alerted(job_id: str, flag_type: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as session:
            flag = (
                session.query(OcaFlag)
                .filter_by(job_id=job_id, flag_type=flag_type, resolved_at=None)
                .first()
            )
            if not flag:
                return False
            flag.last_alerted_at = datetime.now(timezone.utc)
            flag.alert_count = (flag.alert_count or 0) + 1
            session.commit()
            return True
    except Exception as e:
        logger.error(f"DB error in update_flag_alerted: {e}")
        return False


# Keep old name as alias for backward compatibility with alerts.py stub
def resolve_flags_not_in(active_job_ids: list, flag_type: str) -> None:
    if not _db_available:
        return
    now = datetime.now(timezone.utc)
    try:
        with _Session() as session:
            flags = (
                session.query(OcaFlag)
                .filter(
                    OcaFlag.flag_type == flag_type,
                    OcaFlag.resolved_at == None,
                    OcaFlag.job_id.notin_(active_job_ids) if active_job_ids else True,
                )
                .all()
            )
            for flag in flags:
                flag.resolved_at = now
                logger.info(f"Auto-resolved {flag_type} flag for {flag.job_id}")
            session.commit()
    except Exception as e:
        logger.error(f"DB error in resolve_flags_not_in ({flag_type}): {e}")


# --- Fix 3: per-customer comms hold ---

def set_comms_hold(job_id: int, reason: str, hold_until: "date | None" = None) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as s:
            row = s.query(CaseyActiveJob).filter_by(id=job_id).first()
            if not row:
                return False
            row.comms_hold = True
            row.comms_hold_reason = reason.strip()
            row.comms_hold_set_at = datetime.now(timezone.utc)
            row.comms_hold_until = hold_until
            s.commit()
            return True
    except Exception as e:
        logger.error(f"set_comms_hold({job_id}): {e}")
        return False


def clear_comms_hold(job_id: int) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as s:
            row = s.query(CaseyActiveJob).filter_by(id=job_id).first()
            if not row:
                return False
            row.comms_hold = False
            row.comms_hold_reason = None
            row.comms_hold_set_at = None
            row.comms_hold_until = None
            s.commit()
            return True
    except Exception as e:
        logger.error(f"clear_comms_hold({job_id}): {e}")
        return False


# --- Sheet reconciliation: soft-delete jobs removed from sheet ---

def count_retirement_candidates(sheet_keys: set) -> "tuple[int, int]":
    """Read-only preview for reconcile_retired_jobs()'s retire guard.

    Returns (candidate_count, total_active_count): how many non-retired
    to_start/in_process rows would be retired by the given sheet_keys,
    and how many such rows exist in total right now. Does not mutate
    anything — safe to call before deciding whether to actually retire.
    """
    if not _db_available:
        active = [
            job for key, job in _memory_jobs.items()
            if key[1] in ("in_process", "to_start") and not job.get("retired")
        ]
        candidates = sum(1 for key, job in _memory_jobs.items()
                          if key[1] in ("in_process", "to_start")
                          and not job.get("retired")
                          and key not in sheet_keys)
        return candidates, len(active)

    try:
        with _Session() as s:
            rows = (
                s.query(CaseyActiveJob)
                .filter(
                    CaseyActiveJob.sheet_tab.in_(["in_process", "to_start"]),
                    (CaseyActiveJob.retired == False) | (CaseyActiveJob.retired == None),
                )
                .all()
            )
            total_active_count = len(rows)
            candidate_count = sum(
                1 for row in rows
                if (row.client_name, row.sheet_tab or "", row.primary_job_type or "") not in sheet_keys
            )
            return candidate_count, total_active_count
    except Exception as e:
        logger.error(f"DB error in count_retirement_candidates: {e}")
        return 0, 0


def reconcile_retired_jobs(sheet_keys: set) -> int:
    """Retire all non-retired active-tab rows whose (client_name, sheet_tab, primary_job_type)
    key is absent from the current sheet fetch. Returns count of newly retired rows."""
    now = datetime.now(timezone.utc)
    retired_count = 0

    if not _db_available:
        for key, job in list(_memory_jobs.items()):
            client_name, sheet_tab, primary_job_type = key
            if sheet_tab not in ("in_process", "to_start"):
                continue
            if key not in sheet_keys and not job.get("retired"):
                job["retired"] = True
                job["retired_at"] = now
                logger.info(f"RETIRED {client_name} — no longer present in sheet, removing from active processing")
                retired_count += 1
        return retired_count

    try:
        with _Session() as s:
            rows = (
                s.query(CaseyActiveJob)
                .filter(
                    CaseyActiveJob.sheet_tab.in_(["in_process", "to_start"]),
                    (CaseyActiveJob.retired == False) | (CaseyActiveJob.retired == None),
                )
                .all()
            )
            for row in rows:
                key = (row.client_name, row.sheet_tab or "", row.primary_job_type or "")
                if key not in sheet_keys:
                    row.retired = True
                    row.retired_at = now
                    logger.info(f"RETIRED {row.client_name} — no longer present in sheet, removing from active processing")
                    retired_count += 1
            s.commit()
    except Exception as e:
        logger.error(f"DB error in reconcile_retired_jobs: {e}")
    return retired_count


# --- Ingestion cursors (agents/casey/ingest_windows.py callers) ------------

def get_ingest_cursor(source: str) -> "str | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            row = s.query(IngestCursor).filter_by(source=source).first()
            return row.cursor_value if row else None
    except Exception as e:
        logger.error(f"get_ingest_cursor({source}): {e}")
        return None


def set_ingest_cursor(source: str, cursor_value: str) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(IngestCursor).filter_by(source=source).first()
            if row:
                row.cursor_value = cursor_value
                row.updated_at = datetime.now(timezone.utc)
            else:
                s.add(IngestCursor(source=source, cursor_value=cursor_value))
            s.commit()
    except Exception as e:
        logger.error(f"set_ingest_cursor({source}): {e}")


def create_ingest_run_stats(row: dict) -> "int | None":
    """One row per mailbox (Gmail) or PM (Dialpad) per ingest run — see
    IngestRunStats. Never raises; an audit/reporting write failing here
    must never block the ingest run itself."""
    if not _db_available:
        return None
    try:
        with _Session() as s:
            entry = IngestRunStats(
                source=row["source"], mailbox_or_pm=row.get("mailbox_or_pm"),
                started_at=row["started_at"], finished_at=row.get("finished_at"),
                counters=row.get("counters") or {},
            )
            s.add(entry)
            s.commit()
            return entry.id
    except Exception as e:
        logger.error(f"create_ingest_run_stats({row.get('source')}, {row.get('mailbox_or_pm')}): {e}")
        return None


# --- customer_messages (Casey ingestion agents) -----------------------------

_CUSTOMER_MESSAGE_FIELDS = {
    "source", "external_id", "thread_id", "job_id", "job_key",
    "customer_email", "customer_phone", "duration_ms", "call_state",
    "direction", "occurred_at", "sender", "subject", "body_text",
    "is_automated", "pm_name", "estimator_name", "raw_ref",
    "match_status", "skip_reason",
}


def get_customer_message_raw_ref(source: str, external_id: str) -> "str | None":
    """Fetches an existing row's raw_ref before an upsert, so a caller
    (e.g. ingest_gmail.py, when the same Gmail message is visible from
    two mailboxes) can merge new context into it instead of the upsert
    silently overwriting it."""
    if not _db_available:
        return None
    try:
        with _Session() as s:
            row = s.query(CustomerMessage).filter_by(source=source, external_id=external_id).first()
            return row.raw_ref if row else None
    except Exception as e:
        logger.error(f"get_customer_message_raw_ref({source}, {external_id}): {e}")
        return None


def upsert_customer_message(row: dict) -> bool:
    """Upserts on (source, external_id) — the same call/email/SMS ingested
    twice updates the existing row instead of duplicating it. Returns True
    if a new row was inserted, False if an existing row was updated (or on
    failure) — used by ingestion agents to report "N new rows" without
    double-counting on a re-run."""
    if not _db_available:
        return False
    clean = {k: v for k, v in row.items() if k in _CUSTOMER_MESSAGE_FIELDS}
    try:
        with _Session() as s:
            existing = (
                s.query(CustomerMessage)
                .filter_by(source=clean["source"], external_id=clean["external_id"])
                .first()
            )
            if existing:
                for field, value in clean.items():
                    setattr(existing, field, value)
                s.commit()
                return False
            s.add(CustomerMessage(**clean))
            s.commit()
            return True
    except Exception as e:
        logger.error(f"upsert_customer_message({row.get('source')}, {row.get('external_id')}): {e}")
        return False


# --- casey_response_watches (agents/casey/run_watches.py) -------------------

def find_unwatched_inbound_messages() -> list[dict]:
    """Inbound, non-automated **Gmail** customer_messages rows with no
    existing casey_response_watches row yet. The 24-hour response-watch
    rule is specifically about customer emails (Stage 6) — a Dialpad call
    is answered or not in the call itself, not via a follow-up watch, so
    source must be scoped to "gmail" here."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            watched_ids = {row[0] for row in s.query(CaseyResponseWatch.inbound_message_id).all()}
            rows = (
                s.query(CustomerMessage)
                .filter(
                    CustomerMessage.source == "gmail",
                    CustomerMessage.direction == "inbound",
                    CustomerMessage.is_automated == False,
                )
                .all()
            )
            return [
                {
                    "id": r.id, "thread_id": r.thread_id, "job_key": r.job_key,
                    "pm_name": r.pm_name, "customer_email": r.customer_email,
                    "customer_phone": r.customer_phone, "occurred_at": r.occurred_at,
                }
                for r in rows if r.id not in watched_ids
            ]
    except Exception as e:
        logger.error(f"find_unwatched_inbound_messages: {e}")
        return []


def create_response_watch(row: dict) -> "int | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            watch = CaseyResponseWatch(
                thread_id=row.get("thread_id"),
                inbound_message_id=row["inbound_message_id"],
                job_key=row.get("job_key"),
                pm_name=row.get("pm_name"),
                customer_email=row.get("customer_email"),
                received_at=row["received_at"],
                due_at=row["due_at"],
                status="waiting",
            )
            s.add(watch)
            s.commit()
            return watch.id
    except Exception as e:
        logger.error(f"create_response_watch: {e}")
        return None


def get_open_response_watches() -> list[dict]:
    """Watches not yet closed — status waiting or overdue (responded and
    excluded are terminal)."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            rows = (
                s.query(CaseyResponseWatch)
                .filter(CaseyResponseWatch.status.in_(["waiting", "overdue"]), CaseyResponseWatch.is_demo == False)  # noqa: E712
                .all()
            )
            return [
                {
                    "id": r.id, "thread_id": r.thread_id, "inbound_message_id": r.inbound_message_id,
                    "job_key": r.job_key, "pm_name": r.pm_name, "customer_email": r.customer_email,
                    "received_at": r.received_at, "due_at": r.due_at, "status": r.status,
                }
                for r in rows
            ]
    except Exception as e:
        logger.error(f"get_open_response_watches: {e}")
        return []


def get_response_candidates(thread_id: "str | None", job_key: "str | None") -> list[dict]:
    """Candidate responses for response_watch.classify_response(): every
    outbound Gmail message in the same thread, plus every connected
    (duration_ms > 0) outbound Dialpad call tied to the same job_key."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            candidates: list[dict] = []
            if thread_id:
                gmail_rows = (
                    s.query(CustomerMessage)
                    .filter(
                        CustomerMessage.source == "gmail",
                        CustomerMessage.direction == "outbound",
                        CustomerMessage.thread_id == thread_id,
                    )
                    .all()
                )
                candidates.extend(
                    {"source": "gmail", "direction": "outbound", "thread_id": r.thread_id,
                     "occurred_at": r.occurred_at}
                    for r in gmail_rows
                )
            if job_key:
                call_rows = (
                    s.query(CustomerMessage)
                    .filter(
                        CustomerMessage.source == "dialpad_call",
                        CustomerMessage.direction == "outbound",
                        CustomerMessage.job_key == job_key,
                        CustomerMessage.duration_ms > 0,
                    )
                    .all()
                )
                from agents.casey.normalizers import classify_call
                for r in call_rows:
                    classification = classify_call(r.direction, r.duration_ms, (r.body_text or "").splitlines())
                    candidates.append({
                        "source": "dialpad_call", "direction": "outbound", "job_key": r.job_key,
                        "occurred_at": r.occurred_at, "duration_ms": r.duration_ms, "message_id": r.id,
                        "is_automated": r.is_automated, "customer_words": classification["customer_words"],
                    })
            return candidates
    except Exception as e:
        logger.error(f"get_response_candidates: {e}")
        return []


def update_response_watch(watch_id: int, status: str, responded_at=None, response_message_id=None) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(CaseyResponseWatch).filter_by(id=watch_id).first()
            if not row:
                return
            row.status = status
            if responded_at is not None:
                row.responded_at = responded_at
            if response_message_id is not None:
                row.response_message_id = response_message_id
            s.commit()
    except Exception as e:
        logger.error(f"update_response_watch({watch_id}): {e}")


# --- casey_notifications (planning only — never sent from here) ------------

def create_notification(row: dict) -> "int | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            notification = CaseyNotification(
                flag_id=row.get("flag_id"),
                watch_id=row.get("watch_id"),
                channel=row["channel"],
                recipient=row["recipient"],
                severity=row.get("severity"),
                dedup_key=row["dedup_key"],
                status=row.get("status", "planned"),
                reason=row.get("reason"),
                message=row.get("message"),
            )
            s.add(notification)
            s.commit()
            return notification.id
    except Exception as e:
        logger.error(f"create_notification(dedup_key={row.get('dedup_key')}): {e}")
        return None


def notification_exists(dedup_key: str) -> bool:
    if not _db_available:
        return False
    try:
        with _Session() as s:
            return s.query(CaseyNotification).filter_by(dedup_key=dedup_key).first() is not None
    except Exception as e:
        logger.error(f"notification_exists({dedup_key}): {e}")
        return False


# --- Sending (agents/casey/notify.py) ---------------------------------------

def _notification_to_dict(r) -> dict:
    return {
        "id": r.id, "flag_id": r.flag_id, "watch_id": r.watch_id, "channel": r.channel,
        "recipient": r.recipient, "severity": r.severity, "dedup_key": r.dedup_key,
        "status": r.status, "reason": r.reason, "message": r.message, "attempts": r.attempts,
    }


def get_planned_notifications(limit: int = 50) -> list[dict]:
    if not _db_available:
        return []
    try:
        with _Session() as s:
            rows = (
                s.query(CaseyNotification)
                .filter(CaseyNotification.status == "planned")
                .order_by(CaseyNotification.planned_at.asc())
                .limit(limit)
                .all()
            )
            return [_notification_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_planned_notifications: {e}")
        return []


def claim_notification_for_sending(notification_id: int) -> "dict | None":
    """Atomically moves one row from "planned" to "sending" and returns
    it, or None if it was already claimed (by this or another process) —
    the UPDATE's WHERE clause only matches a still-"planned" row, so two
    concurrent callers racing on the same id can never both succeed."""
    if not _db_available:
        return None
    try:
        with _Session() as s:
            result = s.execute(
                text("""
                    UPDATE casey_notifications SET status = 'sending'
                    WHERE id = :id AND status = 'planned'
                    RETURNING id, flag_id, watch_id, channel, recipient, severity,
                              dedup_key, status, reason, message, attempts
                """),
                {"id": notification_id},
            ).fetchone()
            s.commit()
            return dict(result._mapping) if result else None
    except Exception as e:
        logger.error(f"claim_notification_for_sending({notification_id}): {e}")
        return None


def mark_notification_sent(notification_id: int) -> None:
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(CaseyNotification).filter_by(id=notification_id).first()
            if not row:
                return
            row.status = "sent"
            row.sent_at = datetime.now(timezone.utc)
            row.error = None
            s.commit()
    except Exception as e:
        logger.error(f"mark_notification_sent({notification_id}): {e}")


def mark_notification_failed(notification_id: int, error: str, max_retries: int = 3) -> str:
    """Increments attempts; returns the row to "planned" (for the next
    run to retry) while attempts stays under max_retries, otherwise
    marks it "failed" permanently. Returns the resulting status."""
    if not _db_available:
        return "failed"
    try:
        with _Session() as s:
            row = s.query(CaseyNotification).filter_by(id=notification_id).first()
            if not row:
                return "failed"
            row.attempts = (row.attempts or 0) + 1
            row.error = error
            row.status = "planned" if row.attempts < max_retries else "failed"
            s.commit()
            return row.status
    except Exception as e:
        logger.error(f"mark_notification_failed({notification_id}): {e}")
        return "failed"


# --- Analysis engine (agents/casey/analyze.py) ------------------------------

def _customer_message_to_dict(r) -> dict:
    return {
        "id": r.id, "source": r.source, "external_id": r.external_id, "thread_id": r.thread_id,
        "job_id": r.job_id, "job_key": r.job_key, "customer_email": r.customer_email,
        "customer_phone": r.customer_phone, "direction": r.direction, "occurred_at": r.occurred_at,
        "sender": r.sender, "subject": r.subject, "body_text": r.body_text or "",
        "is_automated": r.is_automated, "pm_name": r.pm_name, "estimator_name": r.estimator_name,
        "match_status": r.match_status, "skip_reason": r.skip_reason,
    }


def get_messages_to_analyze(limit: int = 10, only_matched: bool = True) -> list[dict]:
    """Candidates for agents/casey/analyze.py, oldest first (deterministic
    FIFO), capped at `limit`. only_matched=True restricts to rows with a
    resolved job_id (never analyzes a message Casey can't tie to a
    project).

    Channel rules (Stage 10a): Gmail is analyzed inbound only — an
    outbound email is stored as metadata only and has no body to score.
    A Dialpad call is analyzed in EITHER direction (the model only ever
    scores the customer's lines within it; see
    agents/casey/analyze.py::split_call_transcript). Never automated
    (voicemail/greeting) and never skip_reason (e.g. no_customer_text,
    or an outgoing-email metadata-only row). SMS is out of scope until
    its own stage, so it's excluded by only naming gmail/dialpad_call
    here rather than relying on direction/skip_reason alone."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            analyzed_ids = {row[0] for row in s.query(MessageAnalysis.message_id).all()}
            q = s.query(CustomerMessage).filter(
                CustomerMessage.source.in_(["gmail", "dialpad_call"]),
                or_(
                    and_(CustomerMessage.source == "gmail", CustomerMessage.direction == "inbound"),
                    CustomerMessage.source == "dialpad_call",
                ),
                CustomerMessage.is_automated == False,
                CustomerMessage.skip_reason.is_(None),
                CustomerMessage.is_demo == False,
            )
            if only_matched:
                q = q.filter(CustomerMessage.job_id.isnot(None))
            q = q.order_by(CustomerMessage.occurred_at.asc())
            rows = [r for r in q.all() if r.id not in analyzed_ids]
            return [_customer_message_to_dict(r) for r in rows[:limit]]
    except Exception as e:
        logger.error(f"get_messages_to_analyze: {e}")
        return []


def get_message_history(job_key: "str | None", before, limit: int = 10, since_days: int = 60) -> list[dict]:
    """Up to `limit` customer_messages for the same job_key, occurring in
    [before - since_days, before), oldest first, each paired with its
    latest analysis's sentiment/summary (None if not yet analyzed)."""
    if not _db_available or not job_key:
        return []
    try:
        with _Session() as s:
            cutoff = before - timedelta(days=since_days)
            rows = (
                s.query(CustomerMessage)
                .filter(
                    CustomerMessage.job_key == job_key,
                    CustomerMessage.occurred_at < before,
                    CustomerMessage.occurred_at >= cutoff,
                    CustomerMessage.is_demo == False,
                )
                .order_by(CustomerMessage.occurred_at.desc())
                .limit(limit)
                .all()
            )
            rows = list(reversed(rows))  # oldest first
            message_ids = [r.id for r in rows]
            analyses = {}
            if message_ids:
                for a in (
                    s.query(MessageAnalysis)
                    .filter(MessageAnalysis.message_id.in_(message_ids))
                    .order_by(MessageAnalysis.created_at.asc())
                    .all()
                ):
                    analyses[a.message_id] = a  # last one wins if re-analyzed
            return [
                {
                    "id": r.id, "occurred_at": r.occurred_at,
                    "sentiment": (analyses.get(r.id).sentiment if r.id in analyses else None),
                    "summary": (analyses.get(r.id).summary if r.id in analyses else None),
                }
                for r in rows
            ]
    except Exception as e:
        logger.error(f"get_message_history({job_key}): {e}")
        return []


def get_open_flag_for_job(job_key: "str | None") -> "dict | None":
    if not _db_available or not job_key:
        return None
    try:
        with _Session() as s:
            row = (
                s.query(CaseyFlag)
                .filter(CaseyFlag.job_key == job_key, CaseyFlag.closed_at.is_(None), CaseyFlag.is_demo == False)  # noqa: E712
                .order_by(CaseyFlag.created_at.desc())
                .first()
            )
            if not row:
                return None
            return {
                "id": row.id, "severity": row.severity, "primary_reason": row.primary_reason,
                "issue_summary": row.issue_summary, "dedup_key": row.dedup_key,
            }
    except Exception as e:
        logger.error(f"get_open_flag_for_job({job_key}): {e}")
        return None


def save_analysis(row: dict) -> "int | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            analysis = MessageAnalysis(
                message_id=row["message_id"],
                model=row.get("model"),
                prompt_version=row.get("prompt_version"),
                sentiment=row.get("sentiment"),
                sentiment_score=row.get("sentiment_score"),
                is_strong_praise=row.get("is_strong_praise", False),
                red_flag_suggested=row.get("red_flag_suggested"),
                suggested_severity=row.get("suggested_severity"),
                suggested_primary_reason=row.get("suggested_primary_reason"),
                suggested_reasons=row.get("suggested_reasons"),
                reason_notes=row.get("reason_notes"),
                suggested_root_cause=row.get("suggested_root_cause"),
                summary=row.get("summary"),
                evidence_quote=row.get("evidence_quote"),
                same_complaint_as_open_flag=row.get("same_complaint_as_open_flag"),
                trajectory=row.get("trajectory"),
                confidence=row.get("confidence"),
                status=row.get("status", "ok"),
                raw_json=row.get("raw_json"),
            )
            s.add(analysis)
            s.commit()
            return analysis.id
    except Exception as e:
        logger.error(f"save_analysis(message_id={row.get('message_id')}): {e}")
        return None


def get_unprocessed_analyses() -> list[dict]:
    """message_analyses rows with red_flag_suggested=True, status="ok",
    that haven't yet produced a casey_flag_event (agents/casey/
    process_flags.py tracks "processed" via a flag_event referencing the
    message_id)."""
    if not _db_available:
        return []
    try:
        with _Session() as s:
            processed_message_ids = {
                row[0] for row in s.query(CaseyFlagEvent.message_id).filter(CaseyFlagEvent.message_id.isnot(None)).all()
            }
            rows = (
                s.query(MessageAnalysis)
                .filter(MessageAnalysis.red_flag_suggested == True, MessageAnalysis.status == "ok",  # noqa: E712
                        MessageAnalysis.is_demo == False)
                .order_by(MessageAnalysis.created_at.asc())
                .all()
            )
            result = []
            for a in rows:
                if a.message_id in processed_message_ids:
                    continue
                message = s.query(CustomerMessage).filter_by(id=a.message_id).first()
                if not message:
                    continue
                result.append({
                    "analysis_id": a.id, "message_id": a.message_id,
                    "job_id": message.job_id, "job_key": message.job_key,
                    "customer_name": message.customer_email, "pm_name": message.pm_name,
                    "estimator_name": message.estimator_name,
                    "severity": a.suggested_severity, "primary_reason": a.suggested_primary_reason,
                    "additional_reasons": a.suggested_reasons or [], "root_cause": a.suggested_root_cause,
                    "reason_notes": a.reason_notes, "issue_summary": a.summary,
                    "sentiment": a.sentiment, "occurred_at": message.occurred_at,
                })
            return result
    except Exception as e:
        logger.error(f"get_unprocessed_analyses: {e}")
        return []


# --- casey_flags / casey_flag_events (agents/casey/process_flags.py) -------

def _flag_to_dict(r) -> dict:
    return {
        "id": r.id, "job_id": r.job_id, "job_key": r.job_key, "customer_name": r.customer_name,
        "address": r.address, "pm_name": r.pm_name, "estimator_name": r.estimator_name,
        "severity": r.severity, "status": r.status, "primary_reason": r.primary_reason,
        "additional_reasons": r.additional_reasons or [], "root_cause": r.root_cause,
        "reason_notes": r.reason_notes, "issue_summary": r.issue_summary,
        "follow_up_required": r.follow_up_required, "dedup_key": r.dedup_key,
        "first_message_id": r.first_message_id, "latest_message_id": r.latest_message_id,
        "created_at": r.created_at, "updated_at": r.updated_at, "closed_at": r.closed_at,
    }


def get_open_flags_for_job(job_key: "str | None") -> list[dict]:
    """Every non-closed flag for this job — there can be more than one
    at once, one per distinct underlying complaint (create_separate)."""
    if not _db_available or not job_key:
        return []
    try:
        with _Session() as s:
            rows = (
                s.query(CaseyFlag)
                .filter(CaseyFlag.job_key == job_key, CaseyFlag.closed_at.is_(None), CaseyFlag.is_demo == False)  # noqa: E712
                .order_by(CaseyFlag.created_at.asc())
                .all()
            )
            return [_flag_to_dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_open_flags_for_job({job_key}): {e}")
        return []


def get_flag_by_id(flag_id: "int | None") -> "dict | None":
    if not _db_available or flag_id is None:
        return None
    try:
        with _Session() as s:
            row = s.query(CaseyFlag).filter_by(id=flag_id).first()
            return _flag_to_dict(row) if row else None
    except Exception as e:
        logger.error(f"get_flag_by_id({flag_id}): {e}")
        return None


def create_flag_row(row: dict) -> "int | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            flag = CaseyFlag(
                job_id=row.get("job_id"), job_key=row.get("job_key"),
                customer_name=row.get("customer_name"), address=row.get("address"),
                pm_name=row.get("pm_name"), estimator_name=row.get("estimator_name"),
                severity=row["severity"], status=row.get("status", "Open"),
                primary_reason=row.get("primary_reason"), additional_reasons=row.get("additional_reasons") or [],
                root_cause=row.get("root_cause", "Undetermined (default)"), reason_notes=row.get("reason_notes"),
                issue_summary=row.get("issue_summary"), follow_up_required=row.get("follow_up_required", True),
                dedup_key=row["dedup_key"], first_message_id=row.get("first_message_id"),
                latest_message_id=row.get("latest_message_id"),
            )
            s.add(flag)
            s.commit()
            return flag.id
    except Exception as e:
        logger.error(f"create_flag_row(dedup_key={row.get('dedup_key')}): {e}")
        return None


def update_flag_row(flag_id: int, **fields) -> None:
    """Updates whichever columns are passed (e.g. severity=..., status=...,
    latest_message_id=...). updated_at is always bumped."""
    if not _db_available:
        return
    try:
        with _Session() as s:
            row = s.query(CaseyFlag).filter_by(id=flag_id).first()
            if not row:
                return
            for key, value in fields.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = datetime.now(timezone.utc)
            s.commit()
    except Exception as e:
        logger.error(f"update_flag_row({flag_id}): {e}")


def create_flag_event(row: dict) -> "int | None":
    if not _db_available:
        return None
    try:
        with _Session() as s:
            event = CaseyFlagEvent(
                flag_id=row.get("flag_id"), event_type=row["event_type"],
                old_value=row.get("old_value"), new_value=row.get("new_value"),
                actor=row.get("actor", "casey"), message_id=row.get("message_id"),
            )
            s.add(event)
            s.commit()
            return event.id
    except Exception as e:
        logger.error(f"create_flag_event(event_type={row.get('event_type')}): {e}")
        return None

