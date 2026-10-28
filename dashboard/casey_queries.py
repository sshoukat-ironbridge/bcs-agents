"""Aggregation query layer for the Casey dashboard (/casey/*). Every
function takes `viewer` ({"role": ..., "linked_name": ...}) and applies
agents/casey/access.py's role-based visibility — a pm/estimator viewer
only ever sees rows for their own projects. Every function also respects
demo mode (db.state_store.casey_demo_mode()): it returns ONLY is_demo
rows when demo mode is on, and ONLY real (is_demo=False) rows when it is
off. No function here writes to the database.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

from agents.casey.taxonomy import SEVERITIES, STATUSES

MIN_SAMPLE_FOR_RATE = 5
SMALL_SAMPLE_LABEL = "n/a (small sample)"

AGING_BUCKET_LABELS = ["0-3", "4-7", "8-14", "15+"]


def _aging_bucket(days: int) -> str:
    if days <= 3:
        return "0-3"
    if days <= 7:
        return "4-7"
    if days <= 14:
        return "8-14"
    return "15+"


def _role_filter(viewer: "dict | None") -> "tuple[str, str | None]":
    """Returns ("full", None): see everything; ("field", "pm_name"|
    "estimator_name"): restrict to rows whose that field equals
    viewer["linked_name"]; ("none", None): see nothing (unknown role, or
    a restricted role with no linked_name — fail closed, matching
    agents/casey/access.py's own default)."""
    from agents.casey.access import FULL_VISIBILITY_ROLES, visibility_field

    viewer = viewer or {}
    role = viewer.get("role", "")
    linked_name = (viewer.get("linked_name") or "").strip()
    if role in FULL_VISIBILITY_ROLES:
        return "full", None
    field = visibility_field(role)
    if field and linked_name:
        return "field", field
    return "none", None


def _week_buckets(weeks: int, now: "datetime | None" = None) -> list:
    """Returns `weeks` (start, end, label) tuples, oldest first, each a
    Monday-to-Monday UTC week; the last one is the current week."""
    now = now or datetime.now(timezone.utc)
    start_of_this_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    buckets = []
    for i in range(weeks - 1, -1, -1):
        start = start_of_this_week - timedelta(weeks=i)
        end = start + timedelta(weeks=1)
        buckets.append((start, end, start.strftime("%b %-d")))
    return buckets


def _rate_or_small_sample(total, count, ndigits=1):
    if count < MIN_SAMPLE_FOR_RATE:
        return SMALL_SAMPLE_LABEL
    return round(total / count, ndigits)


# --- KPIs --------------------------------------------------------------------

def kpis(viewer: "dict | None" = None) -> dict[str, Any]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag, CaseyResponseWatch, MessageAnalysis, CustomerMessage

    empty = {
        "open_flags": 0, "critical_high_count": 0, "flagged_projects": 0,
        "avg_age_days": SMALL_SAMPLE_LABEL, "avg_days_to_close": SMALL_SAMPLE_LABEL,
        "positive_praise_this_month": 0, "overdue_responses": 0,
    }
    if not _db_available:
        return empty

    mode, field = _role_filter(viewer)
    if mode == "none":
        return empty
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            now = datetime.now(timezone.utc)

            flag_q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo)
            if mode == "field":
                flag_q = flag_q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])

            open_flags = flag_q.filter(CaseyFlag.closed_at.is_(None)).all()
            open_count = len(open_flags)
            critical_high = sum(1 for f in open_flags if f.severity in ("Critical", "High"))
            flagged_projects = len({f.job_key for f in open_flags if f.job_key})

            ages = [(now - f.created_at).total_seconds() / 86400 for f in open_flags if f.created_at]
            avg_age_days = _rate_or_small_sample(sum(ages), len(ages)) if ages else SMALL_SAMPLE_LABEL

            closed_flags = flag_q.filter(CaseyFlag.closed_at.isnot(None)).all()
            close_days = [
                (f.closed_at - f.created_at).total_seconds() / 86400
                for f in closed_flags if f.created_at and f.closed_at
            ]
            avg_days_to_close = (
                _rate_or_small_sample(sum(close_days), len(close_days)) if close_days else SMALL_SAMPLE_LABEL
            )

            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            praise_q = (
                s.query(MessageAnalysis)
                .join(CustomerMessage, MessageAnalysis.message_id == CustomerMessage.id)
                .filter(
                    MessageAnalysis.is_demo == demo,
                    MessageAnalysis.is_strong_praise == True,  # noqa: E712
                    MessageAnalysis.created_at >= month_start,
                )
            )
            if mode == "field":
                praise_q = praise_q.filter(getattr(CustomerMessage, field) == viewer["linked_name"])
            positive_praise = praise_q.count()

            watch_q = s.query(CaseyResponseWatch).filter(
                CaseyResponseWatch.is_demo == demo, CaseyResponseWatch.status == "overdue"
            )
            if mode == "field" and field == "pm_name":
                watch_q = watch_q.filter(CaseyResponseWatch.pm_name == viewer["linked_name"])
            elif mode == "field":
                # estimator role: response watches don't carry estimator_name,
                # so there's no way to attribute one to a specific estimator —
                # fail closed rather than guess.
                watch_q = watch_q.filter(False)
            overdue_responses = watch_q.count()

            return {
                "open_flags": open_count, "critical_high_count": critical_high,
                "flagged_projects": flagged_projects, "avg_age_days": avg_age_days,
                "avg_days_to_close": avg_days_to_close,
                "positive_praise_this_month": positive_praise, "overdue_responses": overdue_responses,
            }
    except Exception:
        return empty


# --- Distribution charts -------------------------------------------------------

def flags_by_severity(viewer: "dict | None" = None) -> list[dict]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    counts = {sev: 0 for sev in SEVERITIES}
    if not _db_available:
        return [{"severity": sev, "count": counts[sev]} for sev in SEVERITIES]

    mode, field = _role_filter(viewer)
    if mode == "none":
        return [{"severity": sev, "count": 0} for sev in SEVERITIES]
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo, CaseyFlag.closed_at.is_(None))
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])
            for f in q.all():
                if f.severity in counts:
                    counts[f.severity] += 1
    except Exception:
        pass
    return [{"severity": sev, "count": counts[sev]} for sev in SEVERITIES]


def flags_by_category(viewer: "dict | None" = None, top: int = 8) -> list[dict]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    if not _db_available:
        return []
    mode, field = _role_filter(viewer)
    if mode == "none":
        return []
    demo = casey_demo_mode()

    counts: dict[str, int] = {}
    try:
        with _Session() as s:
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo, CaseyFlag.closed_at.is_(None))
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])
            for f in q.all():
                reason = f.primary_reason or "(unspecified)"
                counts[reason] = counts.get(reason, 0) + 1
    except Exception:
        return []
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"category": k, "count": v} for k, v in ranked[:top]]


def flags_by_status(viewer: "dict | None" = None) -> list[dict]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    counts = {status: 0 for status in STATUSES}
    if not _db_available:
        return [{"status": s, "count": counts[s]} for s in STATUSES]
    mode, field = _role_filter(viewer)
    if mode == "none":
        return [{"status": s, "count": 0} for s in STATUSES]
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo)
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])
            for f in q.all():
                if f.status in counts:
                    counts[f.status] += 1
    except Exception:
        pass
    return [{"status": s, "count": counts[s]} for s in STATUSES]


def aging_buckets(viewer: "dict | None" = None) -> list[dict]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    counts = {label: 0 for label in AGING_BUCKET_LABELS}
    if not _db_available:
        return [{"bucket": b, "count": 0} for b in AGING_BUCKET_LABELS]
    mode, field = _role_filter(viewer)
    if mode == "none":
        return [{"bucket": b, "count": 0} for b in AGING_BUCKET_LABELS]
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            now = datetime.now(timezone.utc)
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo, CaseyFlag.closed_at.is_(None))
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])
            for f in q.all():
                if not f.created_at:
                    continue
                days = (now - f.created_at).days
                counts[_aging_bucket(days)] += 1
    except Exception:
        pass
    return [{"bucket": b, "count": counts[b]} for b in AGING_BUCKET_LABELS]


# --- Trends over time ----------------------------------------------------------

def flags_opened_closed_by_week(viewer: "dict | None" = None, weeks: int = 8) -> list[dict]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    buckets = _week_buckets(weeks)
    empty_rows = [{"week": label, "opened_by_severity": {sev: 0 for sev in SEVERITIES}, "closed": 0}
                  for _, _, label in buckets]
    if not _db_available:
        return empty_rows
    mode, field = _role_filter(viewer)
    if mode == "none":
        return empty_rows
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo)
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])
            flags = q.all()
    except Exception:
        return empty_rows

    rows = []
    for start, end, label in buckets:
        opened_by_severity = {sev: 0 for sev in SEVERITIES}
        closed = 0
        for f in flags:
            if f.created_at and start <= f.created_at < end and f.severity in opened_by_severity:
                opened_by_severity[f.severity] += 1
            if f.closed_at and start <= f.closed_at < end:
                closed += 1
        rows.append({"week": label, "opened_by_severity": opened_by_severity, "closed": closed})
    return rows


def sentiment_by_week(viewer: "dict | None" = None, pm: "str | None" = None, weeks: int = 8) -> list[dict]:
    from db.state_store import _Session, _db_available, casey_demo_mode, MessageAnalysis, CustomerMessage

    buckets = _week_buckets(weeks)
    empty_rows = [{"week": label, "avg_score": None, "positive_count": 0, "negative_count": 0}
                  for _, _, label in buckets]
    if not _db_available:
        return empty_rows
    mode, field = _role_filter(viewer)
    if mode == "none":
        return empty_rows
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            q = (
                s.query(MessageAnalysis, CustomerMessage)
                .join(CustomerMessage, MessageAnalysis.message_id == CustomerMessage.id)
                .filter(MessageAnalysis.is_demo == demo)
            )
            if mode == "field":
                q = q.filter(getattr(CustomerMessage, field) == viewer["linked_name"])
            if pm:
                q = q.filter(CustomerMessage.pm_name == pm)
            pairs = q.all()
    except Exception:
        return empty_rows

    rows = []
    for start, end, label in buckets:
        scores = []
        positive = negative = 0
        for analysis, message in pairs:
            if not (message.occurred_at and start <= message.occurred_at < end):
                continue
            if analysis.sentiment_score is not None:
                scores.append(analysis.sentiment_score)
            if analysis.sentiment == "positive":
                positive += 1
            elif analysis.sentiment == "negative":
                negative += 1
        avg_score = round(sum(scores) / len(scores), 2) if scores else None
        rows.append({"week": label, "avg_score": avg_score, "positive_count": positive, "negative_count": negative})
    return rows


def filter_options(viewer: "dict | None" = None) -> dict[str, list[str]]:
    """Distinct pm_name/estimator_name/primary_reason values among the
    flags this viewer can see, for building the tracker's filter
    dropdowns from real options only."""
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    empty = {"pm": [], "estimator": [], "category": []}
    if not _db_available:
        return empty
    mode, field = _role_filter(viewer)
    if mode == "none":
        return empty
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo)
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])
            pms, estimators, categories = set(), set(), set()
            for f in q.all():
                if f.pm_name:
                    pms.add(f.pm_name)
                if f.estimator_name:
                    estimators.add(f.estimator_name)
                if f.primary_reason:
                    categories.add(f.primary_reason)
            return {"pm": sorted(pms), "estimator": sorted(estimators), "category": sorted(categories)}
    except Exception:
        return empty


# --- Flags list / detail -------------------------------------------------------

_SORT_COLUMNS = {
    "created_at": "created_at", "updated_at": "updated_at", "severity": "severity",
    "status": "status", "customer_name": "customer_name", "pm_name": "pm_name",
}


def flags_list(viewer: "dict | None" = None, filters: "dict | None" = None,
                sort: str = "created_at", sort_dir: str = "desc",
                page: int = 1, page_size: int = 25) -> dict[str, Any]:
    from db.state_store import _Session, _db_available, casey_demo_mode, CaseyFlag

    empty = {"rows": [], "total": 0, "page": page, "page_size": page_size}
    if not _db_available:
        return empty
    mode, field = _role_filter(viewer)
    if mode == "none":
        return empty
    demo = casey_demo_mode()
    filters = filters or {}

    try:
        with _Session() as s:
            q = s.query(CaseyFlag).filter(CaseyFlag.is_demo == demo)
            if mode == "field":
                q = q.filter(getattr(CaseyFlag, field) == viewer["linked_name"])

            if filters.get("severity"):
                q = q.filter(CaseyFlag.severity == filters["severity"])
            if filters.get("status"):
                q = q.filter(CaseyFlag.status == filters["status"])
            if filters.get("pm"):
                q = q.filter(CaseyFlag.pm_name == filters["pm"])
            if filters.get("estimator"):
                q = q.filter(CaseyFlag.estimator_name == filters["estimator"])
            if filters.get("category"):
                q = q.filter(CaseyFlag.primary_reason == filters["category"])
            search = (filters.get("search") or "").strip()
            if search:
                like = f"%{search}%"
                q = q.filter(
                    (CaseyFlag.customer_name.ilike(like))
                    | (CaseyFlag.address.ilike(like))
                    | (CaseyFlag.issue_summary.ilike(like))
                )

            total = q.count()

            sort_col = getattr(CaseyFlag, _SORT_COLUMNS.get(sort, "created_at"))
            sort_col = sort_col.desc() if sort_dir == "desc" else sort_col.asc()
            rows = (
                q.order_by(sort_col)
                .offset(max(page - 1, 0) * page_size)
                .limit(page_size)
                .all()
            )

            now = datetime.now(timezone.utc)
            result_rows = [
                {
                    "id": f.id, "severity": f.severity, "status": f.status,
                    "customer_name": f.customer_name, "address": f.address,
                    "pm_name": f.pm_name, "estimator_name": f.estimator_name,
                    "primary_reason": f.primary_reason, "age_days": (now - f.created_at).days if f.created_at else None,
                    "updated_at": f.updated_at, "created_at": f.created_at,
                }
                for f in rows
            ]
            return {"rows": result_rows, "total": total, "page": page, "page_size": page_size}
    except Exception:
        return empty


def flag_detail(viewer: "dict | None" = None, flag_id: "int | None" = None) -> "dict | None":
    from db.state_store import (
        _Session, _db_available, casey_demo_mode, CaseyFlag, CaseyFlagEvent, CustomerMessage, MessageAnalysis,
    )

    if not _db_available or flag_id is None:
        return None
    mode, field = _role_filter(viewer)
    if mode == "none":
        return None
    demo = casey_demo_mode()

    try:
        with _Session() as s:
            f = s.query(CaseyFlag).filter(CaseyFlag.id == flag_id, CaseyFlag.is_demo == demo).first()
            if not f:
                return None
            if mode == "field" and (getattr(f, field) or "").strip() != (viewer["linked_name"] or "").strip():
                return None  # exists, but not this viewer's project

            events = (
                s.query(CaseyFlagEvent)
                .filter(CaseyFlagEvent.flag_id == f.id)
                .order_by(CaseyFlagEvent.created_at.asc())
                .all()
            )

            messages = []
            trajectory = []
            if f.job_key:
                rows = (
                    s.query(CustomerMessage, MessageAnalysis)
                    .outerjoin(MessageAnalysis, MessageAnalysis.message_id == CustomerMessage.id)
                    .filter(CustomerMessage.job_key == f.job_key, CustomerMessage.is_demo == demo)
                    .order_by(CustomerMessage.occurred_at.asc())
                    .all()
                )
                for message, analysis in rows:
                    snippet = (message.body_text or "")[:160]
                    messages.append({
                        "id": message.id, "occurred_at": message.occurred_at,
                        "direction": message.direction, "snippet": snippet,
                        "sentiment": analysis.sentiment if analysis else None,
                    })
                    if analysis and analysis.sentiment_score is not None:
                        trajectory.append({"occurred_at": message.occurred_at, "score": analysis.sentiment_score})

            evidence_quote = None
            if f.first_message_id:
                a = (
                    s.query(MessageAnalysis)
                    .filter(MessageAnalysis.message_id == f.first_message_id)
                    .order_by(MessageAnalysis.created_at.desc())
                    .first()
                )
                if a:
                    evidence_quote = a.evidence_quote

            now = datetime.now(timezone.utc)
            return {
                "id": f.id, "severity": f.severity, "status": f.status,
                "customer_name": f.customer_name, "address": f.address, "job_key": f.job_key,
                "pm_name": f.pm_name, "estimator_name": f.estimator_name,
                "primary_reason": f.primary_reason, "additional_reasons": f.additional_reasons or [],
                "root_cause": f.root_cause, "reason_notes": f.reason_notes,
                "issue_summary": f.issue_summary, "evidence_quote": evidence_quote,
                "resolution_reason": f.resolution_reason, "notes": f.notes,
                "created_at": f.created_at, "updated_at": f.updated_at, "closed_at": f.closed_at,
                "age_days": (now - f.created_at).days if f.created_at else None,
                "events": [
                    {
                        "event_type": e.event_type, "old_value": e.old_value, "new_value": e.new_value,
                        "actor": e.actor, "created_at": e.created_at,
                    }
                    for e in events
                ],
                "messages": messages,
                "trajectory": trajectory,
            }
    except Exception:
        return None
