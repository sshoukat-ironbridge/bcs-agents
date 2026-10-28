from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agents.casey.taxonomy import SEVERITIES, STATUSES
from utils.timeutil import format_ny

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

PAGE_SIZE = 25

RESOLUTION_REASONS = [
    "Issue resolved to customer's satisfaction",
    "Resolved via rework",
    "Resolved via refund or credit",
    "Customer withdrew complaint",
    "Customer cancelled contract",
    "Other (see notes)",
]


def _viewer(request: Request) -> dict:
    user = getattr(request.state, "user", None) or {}
    return {"role": user.get("role", ""), "linked_name": user.get("linked_name", "")}


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _demo_banner_context() -> dict:
    from db.state_store import casey_demo_mode
    return {"casey_demo_mode": casey_demo_mode()}


@router.get("/casey", response_class=HTMLResponse)
async def casey_overview(request: Request):
    import dashboard.casey_queries as q

    viewer = _viewer(request)
    ctx = {
        "page": "casey-overview",
        **_demo_banner_context(),
        "kpis": q.kpis(viewer),
        "by_severity": q.flags_by_severity(viewer),
        "by_category": q.flags_by_category(viewer),
        "by_status": q.flags_by_status(viewer),
        "aging": q.aging_buckets(viewer),
        "weekly": q.flags_opened_closed_by_week(viewer, weeks=8),
        "sentiment": q.sentiment_by_week(viewer, weeks=8),
        "severities": SEVERITIES,
    }

    needs_attention = q.flags_list(
        viewer, filters={}, sort="severity", sort_dir="desc", page=1, page_size=100
    )["rows"]
    needs_attention = [
        {**row, "age_days": row["age_days"], "updated_str": format_ny(row.get("updated_at"))}
        for row in needs_attention if row["severity"] in ("Critical", "High") and row["status"] not in ("Resolved", "No Action Taken")
    ]
    ctx["needs_attention"] = needs_attention

    return templates.TemplateResponse(request, "casey_overview.html", ctx)


def _parse_filters(request: Request) -> dict:
    qp = request.query_params
    return {
        "severity": qp.get("severity") or "",
        "status": qp.get("status") or "",
        "pm": qp.get("pm") or "",
        "estimator": qp.get("estimator") or "",
        "category": qp.get("category") or "",
        "search": qp.get("search") or "",
    }


@router.get("/casey/flags", response_class=HTMLResponse)
async def casey_flags_tracker(request: Request):
    import dashboard.casey_queries as q

    viewer = _viewer(request)
    filters = _parse_filters(request)
    sort = request.query_params.get("sort") or "created_at"
    sort_dir = request.query_params.get("dir") or "desc"
    try:
        page = max(int(request.query_params.get("page", 1)), 1)
    except ValueError:
        page = 1

    result = q.flags_list(viewer, filters=filters, sort=sort, sort_dir=sort_dir, page=page, page_size=PAGE_SIZE)
    rows = [
        {**row, "age_str": f"{row['age_days']}d" if row["age_days"] is not None else "—",
         "updated_str": format_ny(row.get("updated_at"))}
        for row in result["rows"]
    ]
    total_pages = max((result["total"] + PAGE_SIZE - 1) // PAGE_SIZE, 1)

    ctx = {
        "page": "casey-flags",
        **_demo_banner_context(),
        "rows": rows,
        "total": result["total"],
        "current_page": page,
        "total_pages": total_pages,
        "filters": filters,
        "filter_options": q.filter_options(viewer),
        "severities": SEVERITIES,
        "statuses": STATUSES,
        "sort": sort,
        "sort_dir": sort_dir,
        "query_string": str(request.query_params),
    }

    template_name = "casey_flags_partial.html" if _is_htmx(request) else "casey_flags.html"
    return templates.TemplateResponse(request, template_name, ctx)


@router.get("/casey/flags/{flag_id}", response_class=HTMLResponse)
async def casey_flag_detail(request: Request, flag_id: int, saved: Optional[str] = None):
    import dashboard.casey_queries as q

    viewer = _viewer(request)
    detail = q.flag_detail(viewer, flag_id)
    if not detail:
        return HTMLResponse("<h1>Flag not found</h1>", status_code=404)

    detail["created_str"] = format_ny(detail.get("created_at"))
    detail["updated_str"] = format_ny(detail.get("updated_at"))
    detail["closed_str"] = format_ny(detail.get("closed_at")) if detail.get("closed_at") else ""
    for event in detail["events"]:
        event["created_str"] = format_ny(event.get("created_at"))
    for message in detail["messages"]:
        message["occurred_str"] = format_ny(message.get("occurred_at"))
    for point in detail["trajectory"]:
        point["occurred_at"] = format_ny(point.get("occurred_at"))

    ctx = {
        "page": "casey-flags",
        **_demo_banner_context(),
        "flag": detail,
        "severities": SEVERITIES,
        "statuses": STATUSES,
        "resolution_reasons": RESOLUTION_REASONS,
        "saved": saved == "1",
    }
    return templates.TemplateResponse(request, "casey_flag_detail.html", ctx)


@router.post("/casey/flags/{flag_id}")
async def casey_flag_update(
    request: Request,
    flag_id: int,
    status: Optional[str] = Form(None),
    resolution_reason: Optional[str] = Form(None),
    severity_override: Optional[str] = Form(None),
    severity_note: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    """Casey never changes a status or severity itself — every change
    here is made by the logged-in human, recorded as a casey_flag_events
    row with actor = that user's email and old/new values."""
    from db.state_store import _Session, _db_available, CaseyFlag
    from agents.casey.access import FULL_VISIBILITY_ROLES, visibility_field

    viewer_user = getattr(request.state, "user", None) or {}
    role = viewer_user.get("role", "")
    linked_name = (viewer_user.get("linked_name") or "").strip()
    actor = viewer_user.get("email") or "unknown user"

    if not _db_available:
        return RedirectResponse(f"/casey/flags/{flag_id}", status_code=303)

    status = (status or "").strip()
    resolution_reason = (resolution_reason or "").strip()
    severity_override = (severity_override or "").strip()
    severity_note = (severity_note or "").strip()
    notes = (notes or "").strip()

    if status in ("Resolved", "No Action Taken") and not resolution_reason:
        return RedirectResponse(f"/casey/flags/{flag_id}?saved=0", status_code=303)
    if severity_override and not severity_note:
        return RedirectResponse(f"/casey/flags/{flag_id}?saved=0", status_code=303)

    from db.state_store import create_flag_event

    with _Session() as s:
        row = s.query(CaseyFlag).filter_by(id=flag_id).first()
        if not row:
            return HTMLResponse("<h1>Flag not found</h1>", status_code=404)

        field = visibility_field(role)
        if role not in FULL_VISIBILITY_ROLES and (not field or (getattr(row, field) or "").strip() != linked_name):
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        now = datetime.now(timezone.utc)

        if status and status in STATUSES and status != row.status:
            old_status = row.status
            row.status = status
            row.resolution_reason = resolution_reason or row.resolution_reason
            if status in ("Resolved", "No Action Taken"):
                row.closed_at = now
            else:
                row.closed_at = None
            create_flag_event({
                "flag_id": flag_id, "event_type": "status_changed", "old_value": old_status,
                "new_value": status, "actor": actor,
            })

        if severity_override and severity_override in SEVERITIES and severity_override != row.severity:
            old_severity = row.severity
            row.severity = severity_override
            row.notes = f"{row.notes + chr(10) if row.notes else ''}Severity override: {severity_note}"
            create_flag_event({
                "flag_id": flag_id, "event_type": "severity_lowered_by_human"
                if SEVERITIES.index(severity_override) < SEVERITIES.index(old_severity) else "severity_escalated",
                "old_value": old_severity, "new_value": severity_override, "actor": actor,
            })

        if notes:
            row.notes = f"{row.notes + chr(10) if row.notes else ''}{notes}"
            create_flag_event({
                "flag_id": flag_id, "event_type": "note", "old_value": None, "new_value": notes, "actor": actor,
            })

        row.updated_at = now
        s.commit()

    return RedirectResponse(f"/casey/flags/{flag_id}?saved=1", status_code=303)


@router.get("/casey/people", response_class=HTMLResponse)
async def casey_people(request: Request, type: str = "pm"):
    import dashboard.casey_queries as q

    person_type = type if type in ("pm", "estimator") else "pm"
    viewer = _viewer(request)
    rows = q.people_workload(viewer, person_type)
    rows_sorted_for_chart = sorted(
        [r for r in rows if isinstance(r["flags_per_10"], (int, float))],
        key=lambda r: r["flags_per_10"], reverse=True,
    )

    ctx = {
        "page": "casey-people",
        **_demo_banner_context(),
        "person_type": person_type,
        "rows": rows,
        "chart_rows": rows_sorted_for_chart,
    }
    return templates.TemplateResponse(request, "casey_people.html", ctx)


@router.get("/casey/positive", response_class=HTMLResponse)
async def casey_positive(request: Request):
    import dashboard.casey_queries as q

    viewer = _viewer(request)
    praise = q.strong_praise_list(viewer, weeks=8)
    for p in praise:
        p["occurred_str"] = format_ny(p.get("occurred_at"))

    ctx = {
        "page": "casey-positive",
        **_demo_banner_context(),
        "praise": praise,
        "weekly": q.praise_weekly_counts(viewer, weeks=8),
        "by_pm": q.praise_by_pm(viewer),
    }
    return templates.TemplateResponse(request, "casey_positive.html", ctx)


@router.get("/casey/responses", response_class=HTMLResponse)
async def casey_responses(request: Request):
    import dashboard.casey_queries as q

    viewer = _viewer(request)
    rows = q.response_watches_table(viewer)
    for r in rows:
        r["received_str"] = format_ny(r.get("received_at"))
        r["due_at_iso"] = r["due_at"].isoformat() if r.get("due_at") else ""

    ctx = {
        "page": "casey-responses",
        **_demo_banner_context(),
        "rows": rows,
        "rate_by_pm": q.response_rate_by_pm(viewer),
    }
    return templates.TemplateResponse(request, "casey_responses.html", ctx)


@router.get("/casey/trends", response_class=HTMLResponse)
async def casey_trends(request: Request, weeks: int = 8, pm: str = ""):
    import dashboard.casey_queries as q

    weeks = weeks if weeks in (4, 8, 12) else 8
    viewer = _viewer(request)

    weekly = q.flags_opened_closed_by_week(viewer, weeks=weeks)
    category_weekly = q.flags_by_category_weekly(viewer, weeks=weeks)
    sentiment_traj = q.sentiment_trajectory_by_pm(viewer, weeks=weeks, pm=pm or None)
    worsening = q.worsening_projects(viewer, weeks=weeks)

    ctx = {
        "page": "casey-trends",
        **_demo_banner_context(),
        "weeks": weeks,
        "pm_filter": pm,
        "pm_options": sorted({row["name"] for row in q.people_workload(viewer, "pm")}),
        "weekly": weekly,
        "category_weekly": category_weekly,
        "sentiment_traj": sentiment_traj,
        "worsening": worsening,
        "severities": SEVERITIES,
    }
    return templates.TemplateResponse(request, "casey_trends.html", ctx)
