"""Builds a one file PDF review pack from the LOCAL database so BCS can
check whether Casey's sentiment analysis and 24 hour email response
watch are right. READ ONLY: reads the DB only, no Gmail, Dialpad or
Anthropic calls, no writes to the DB, no sends. Real data only
(is_demo = false; demo mode is ignored here on purpose).

Usage:
    docker compose run --rm -v "$PWD/out:/app/out" casey python scripts/casey_review_pack.py [--since 30d] [--refresh]

--refresh optionally runs the existing Casey stages first (ingest,
analyze, process_flags, run_watches) exactly as agents/casey/main.py
does; default is off (this script itself never calls out to Gmail,
Dialpad or Anthropic).

Never use an em dash or en dash anywhere in the generated text: use
commas, colons or a plain hyphen instead.
"""
import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import landscape, legal  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    NextPageTemplate, PageBreak,
)

from utils.timeutil import format_ny  # noqa: E402

BRAND = "Ironbridge AI Advisory"
HEADER_TEXT = f"Casey Review Pack, prepared by {BRAND} for Blue Collar Scholars"
FOOTER_NOTICE = "Contains customer information, share securely"

RANDOM_SAMPLE_SEED = 42
QUOTE_MAX_CHARS = 220
EXCERPT_MAX_CHARS = 220
NO_REPLY_WORD_LIMIT = 12
ACK_PHRASES = (
    "thanks", "thank you", "ok", "okay", "sounds good", "got it",
    "appreciate it", "will do", "perfect", "great", "understood",
)

NAVY = colors.HexColor("#1e3a5f")
SEVERITY_COLORS = {
    "Low": colors.HexColor("#2563eb"), "Medium": colors.HexColor("#b45309"),
    "High": colors.HexColor("#c2410c"), "Critical": colors.HexColor("#b91c1c"),
}

PAGE_SIZE = landscape(legal)
MARGIN = 0.4 * inch


# --- Pure helpers (no DB access; unit tested) --------------------------------

def truncate(text: "str | None", max_chars: int = QUOTE_MAX_CHARS) -> str:
    """Truncates RAW text by character count. Never HTML-escaped here -
    that happens separately, at the point text is embedded into a PDF
    Paragraph's markup (see clean()), so escaping never skews where the
    220-character cut lands."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def strip_dashes(text: str) -> str:
    """Never an em dash or en dash anywhere in generated text: replace
    with a comma (em dash) or a plain hyphen (en dash, which is mostly
    used as a numeric range like "10-12")."""
    return text.replace("—", ",").replace("–", "-")


def probably_no_reply_needed(text: "str | None") -> bool:
    """Heuristic (stated in the report): the incoming message has fewer
    than NO_REPLY_WORD_LIMIT words, or contains only a thanks/
    acknowledgement phrase, and has no question mark."""
    text = (text or "").strip()
    if "?" in text:
        return False
    words = text.split()
    if len(words) < NO_REPLY_WORD_LIMIT:
        return True
    low = text.lower().strip().strip(".!")
    return any(low == p or low.startswith(p) for p in ACK_PHRASES)


def why_flagged_sentence(due_at: datetime, last_email_at: "datetime | None", last_call_at: "datetime | None") -> str:
    """Generated per row from the same data the agent's response-watch
    logic itself reads (last outgoing email in thread, last connected,
    non-automated call with the customer) - never reimplementing the
    agent's own matching, just narrating what it found."""
    email_str = format_ny(last_email_at) if last_email_at else "none"
    call_str = format_ny(last_call_at) if last_call_at else "none"
    return (
        f"No outgoing email in this thread and no connected call with this customer since {format_ny(due_at)}. "
        f"Last outgoing email to this customer: {email_str}. "
        f"Last connected call: {call_str}."
    )


def review_hint(match_status: "str | None", cross_pm: bool, body_text: "str | None") -> str:
    if match_status == "ambiguous":
        return "Contact shared by several jobs"
    if cross_pm:
        return "Mailbox owner is not the job PM"
    if not (body_text or "").strip():
        return "No hint"
    if probably_no_reply_needed(body_text):
        return "Short message, may need no reply"
    return "Looks like it needs a reply"


def age_bucket(hours_overdue: float) -> str:
    if hours_overdue < 24:
        return "Under 24h late"
    if hours_overdue <= 72:
        return "1 to 3 days"
    return "Over 3 days"


def hours_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


# --- DB access (read only) ----------------------------------------------------

def _job_lookup() -> dict:
    from db.state_store import get_all_active_jobs
    return {j["id"]: j for j in get_all_active_jobs()}


def _message_source_desc(message) -> str:
    if message is None:
        return "Source text unavailable"
    channel = "Email" if message.source == "gmail" else ("Call" if message.source == "dialpad_call" else message.source)
    direction_word = "incoming" if message.direction == "inbound" else "outgoing"
    time_str = format_ny(message.occurred_at)
    if message.source == "dialpad_call":
        minutes = round((message.duration_ms or 0) / 60000)
        return f"{channel}, {direction_word}, {time_str}, {minutes} min"
    return f"{channel}, {direction_word}, {time_str}"


def fetch_flags() -> list[dict]:
    from db.state_store import _Session, CaseyFlag, CaseyFlagEvent, CustomerMessage, MessageAnalysis

    jobs_by_id = _job_lookup()
    with _Session() as s:
        flags = s.query(CaseyFlag).filter(CaseyFlag.is_demo == False).all()  # noqa: E712
        rows = []
        for f in flags:
            job = jobs_by_id.get(f.job_id) or {}
            message = s.query(CustomerMessage).filter_by(id=f.first_message_id).first() if f.first_message_id else None
            analysis = None
            if f.first_message_id:
                analysis = (
                    s.query(MessageAnalysis)
                    .filter(MessageAnalysis.message_id == f.first_message_id)
                    .order_by(MessageAnalysis.created_at.desc())
                    .first()
                )
            escalated = s.query(CaseyFlagEvent).filter(
                CaseyFlagEvent.flag_id == f.id, CaseyFlagEvent.event_type == "severity_escalated"
            ).count() > 0
            related_count = s.query(CaseyFlagEvent).filter(
                CaseyFlagEvent.flag_id == f.id, CaseyFlagEvent.message_id.isnot(None)
            ).count()

            customer_email = message.customer_email if message else (f.customer_name or "")
            customer_name = job.get("client_name") or customer_email or "Unknown"

            rows.append({
                "id": f.id, "severity": f.severity, "status": f.status,
                "customer_name": customer_name, "customer_email": customer_email,
                "job_type": job.get("primary_job_type") or job.get("job_type") or "",
                "address": job.get("address") or f.address or "",
                "sheet_tab": "To Start" if job.get("sheet_tab") == "to_start" else
                             ("In Process" if job.get("sheet_tab") == "in_process" else ""),
                "pm_name": f.pm_name or "", "estimator_name": f.estimator_name or "",
                "source_desc": _message_source_desc(message),
                "primary_reason": f.primary_reason or "",
                "root_cause": f.root_cause or "",
                "issue_summary": f.issue_summary or "",
                "evidence_quote": truncate(analysis.evidence_quote if analysis else "", QUOTE_MAX_CHARS) or "(none)",
                "first_detected": format_ny(f.created_at),
                "last_updated": format_ny(f.updated_at),
                "related_count": related_count,
                "escalated": "Yes" if escalated else "No",
            })

    severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    # Critical, High, Medium, Low; newest first within each severity (id is
    # assigned in creation order, which for a backfill tracks created_at).
    return sorted(rows, key=lambda r: (severity_order.get(r["severity"], 9), -r["id"]))


def _not_flagged_query(s):
    from db.state_store import MessageAnalysis, CustomerMessage
    return (
        s.query(MessageAnalysis, CustomerMessage)
        .join(CustomerMessage, MessageAnalysis.message_id == CustomerMessage.id)
        .filter(
            MessageAnalysis.is_demo == False,  # noqa: E712
            MessageAnalysis.status == "ok",
            MessageAnalysis.red_flag_suggested == False,  # noqa: E712
        )
    )


def fetch_not_flagged_lowest_sentiment(limit: int = 15) -> list[dict]:
    from db.state_store import _Session

    with _Session() as s:
        pairs = _not_flagged_query(s).all()
    pairs = [p for p in pairs if p[0].sentiment_score is not None]
    pairs.sort(key=lambda p: p[0].sentiment_score)
    return [_not_flagged_row(a, m) for a, m in pairs[:limit]]


def fetch_not_flagged_random_sample(limit: int = 20, seed: int = RANDOM_SAMPLE_SEED) -> list[dict]:
    from db.state_store import _Session

    with _Session() as s:
        pairs = _not_flagged_query(s).all()
    rng = random.Random(seed)
    sample = rng.sample(pairs, min(limit, len(pairs)))
    sample.sort(key=lambda p: p[1].occurred_at or datetime.min.replace(tzinfo=timezone.utc))
    return [_not_flagged_row(a, m) for a, m in sample]


def _not_flagged_row(analysis, message) -> dict:
    from db.state_store import get_all_active_jobs
    jobs_by_id = {j["id"]: j for j in get_all_active_jobs()}
    job = jobs_by_id.get(message.job_id) or {}
    return {
        "customer_name": job.get("client_name") or message.customer_email or "Unknown",
        "customer_email": message.customer_email or "",
        "job_type": job.get("primary_job_type") or job.get("job_type") or "",
        "pm_name": message.pm_name or "",
        "date": format_ny(message.occurred_at),
        "source_desc": _message_source_desc(message),
        "sentiment_score": analysis.sentiment_score,
        "excerpt": truncate(message.body_text, EXCERPT_MAX_CHARS),
    }


def fetch_strong_praise(limit: int = 10) -> list[dict]:
    from db.state_store import _Session, MessageAnalysis, CustomerMessage

    with _Session() as s:
        pairs = (
            s.query(MessageAnalysis, CustomerMessage)
            .join(CustomerMessage, MessageAnalysis.message_id == CustomerMessage.id)
            .filter(MessageAnalysis.is_demo == False, MessageAnalysis.is_strong_praise == True)  # noqa: E712
            .order_by(CustomerMessage.occurred_at.desc())
            .limit(limit)
            .all()
        )
    return [
        {
            "quote": truncate(analysis.summary or message.body_text, QUOTE_MAX_CHARS),
            "customer_name": message.sender or message.customer_email or "Unknown",
            "pm_name": message.pm_name or "",
            "date": format_ny(message.occurred_at),
        }
        for analysis, message in pairs
    ]


def fetch_response_watches() -> dict:
    """Returns {"waiting_overdue": [...], "responded_recent": [...]}."""
    from db.state_store import _Session, CaseyResponseWatch, CustomerMessage, get_response_candidates, find_pm, get_pm_list
    import json as _json

    now = datetime.now(timezone.utc)
    pm_list = get_pm_list()

    with _Session() as s:
        open_watches = (
            s.query(CaseyResponseWatch)
            .filter(CaseyResponseWatch.is_demo == False, CaseyResponseWatch.status.in_(["waiting", "overdue"]))  # noqa: E712
            .all()
        )
        responded_watches = (
            s.query(CaseyResponseWatch)
            .filter(CaseyResponseWatch.is_demo == False, CaseyResponseWatch.status == "responded")  # noqa: E712
            .order_by(CaseyResponseWatch.responded_at.desc())
            .limit(15)
            .all()
        )

        open_rows = []
        for w in open_watches:
            inbound = s.query(CustomerMessage).filter_by(id=w.inbound_message_id).first()
            candidates = get_response_candidates(w.thread_id, w.job_key)
            email_candidates = [c for c in candidates if c["source"] == "gmail"]
            call_candidates = [
                c for c in candidates
                if c["source"] == "dialpad_call" and not c.get("is_automated") and (c.get("customer_words") or 0) >= 3
            ]
            last_email_at = max((c["occurred_at"] for c in email_candidates), default=None)
            last_call_at = max((c["occurred_at"] for c in call_candidates), default=None)

            cross_pm = False
            if inbound and inbound.raw_ref:
                try:
                    cross_pm = bool(_json.loads(inbound.raw_ref).get("cross_pm"))
                except (ValueError, TypeError):
                    cross_pm = False

            mailbox_pm = find_pm(w.pm_name or "", pm_list) if w.pm_name else None
            mailbox_email = (mailbox_pm or {}).get("email", "") or "(unknown)"

            if w.status == "overdue":
                hours_overdue = hours_between(w.due_at, now)
                time_display = f"{hours_overdue:.1f}h overdue"
            else:
                hours_overdue = 0.0
                time_display = f"{hours_between(now, w.due_at):.1f}h left"

            open_rows.append({
                "status": w.status,
                "customer_name": inbound.sender if inbound else (w.customer_email or "Unknown"),
                "customer_email": w.customer_email or "",
                "job_type": "", "address": w.job_key or "",
                "pm_name": w.pm_name or "", "mailbox_email": mailbox_email,
                "subject": (inbound.subject if inbound else "") or "(no subject)",
                "received": format_ny(w.received_at), "due": format_ny(w.due_at),
                "time_display": time_display, "hours_overdue": hours_overdue if w.status == "overdue" else -1,
                "why_flagged": why_flagged_sentence(w.due_at, last_email_at, last_call_at),
                "review_hint": review_hint(
                    inbound.match_status if inbound else None, cross_pm, inbound.body_text if inbound else None
                ),
            })

        responded_rows = []
        for w in responded_watches:
            response_message = (
                s.query(CustomerMessage).filter_by(id=w.response_message_id).first() if w.response_message_id else None
            )
            response_type = "Email" if (response_message and response_message.source == "gmail") else \
                             ("Call" if (response_message and response_message.source == "dialpad_call") else "Unknown")
            hours_to_respond = hours_between(w.received_at, w.responded_at) if w.responded_at else None
            responded_rows.append({
                "customer_email": w.customer_email or "", "pm_name": w.pm_name or "",
                "received": format_ny(w.received_at),
                "responded": format_ny(w.responded_at) if w.responded_at else "",
                "response_type": response_type,
                "hours_to_respond": round(hours_to_respond, 1) if hours_to_respond is not None else None,
            })

    # Most overdue first, then waiting.
    overdue_rows = sorted([r for r in open_rows if r["status"] == "overdue"], key=lambda r: -r["hours_overdue"])
    waiting_rows = sorted([r for r in open_rows if r["status"] == "waiting"], key=lambda r: r["due"])
    return {"waiting_overdue": overdue_rows + waiting_rows, "responded_recent": responded_rows}


def fetch_counts() -> dict:
    from db.state_store import _Session, CaseyFlag, CaseyResponseWatch, MessageAnalysis, CustomerMessage, get_all_active_jobs

    with _Session() as s:
        flags_by_severity = {}
        flags_by_status = {}
        for f in s.query(CaseyFlag).filter(CaseyFlag.is_demo == False).all():  # noqa: E712
            flags_by_severity[f.severity] = flags_by_severity.get(f.severity, 0) + 1
            flags_by_status[f.status] = flags_by_status.get(f.status, 0) + 1
        flagged_projects = len({
            f.job_key for f in s.query(CaseyFlag).filter(CaseyFlag.is_demo == False, CaseyFlag.job_key.isnot(None)).all()  # noqa: E712
        })

        watches_by_status = {}
        for w in s.query(CaseyResponseWatch).filter(CaseyResponseWatch.is_demo == False).all():  # noqa: E712
            watches_by_status[w.status] = watches_by_status.get(w.status, 0) + 1

        incoming_analyzed = s.query(MessageAnalysis).join(
            CustomerMessage, MessageAnalysis.message_id == CustomerMessage.id
        ).filter(MessageAnalysis.is_demo == False, CustomerMessage.source == "gmail").count()  # noqa: E712
        calls_analyzed = s.query(MessageAnalysis).join(
            CustomerMessage, MessageAnalysis.message_id == CustomerMessage.id
        ).filter(MessageAnalysis.is_demo == False, CustomerMessage.source == "dialpad_call").count()  # noqa: E712
        strong_praise_count = s.query(MessageAnalysis).filter(
            MessageAnalysis.is_demo == False, MessageAnalysis.is_strong_praise == True  # noqa: E712
        ).count()

    jobs = [j for j in get_all_active_jobs() if not j.get("retired")]
    import scripts.casey_coverage as coverage
    coverage_data = coverage.compute_coverage()

    return {
        "flags_by_severity": flags_by_severity, "flags_by_status": flags_by_status,
        "flagged_projects": flagged_projects, "watches_by_status": watches_by_status,
        "incoming_analyzed": incoming_analyzed, "calls_analyzed": calls_analyzed,
        "strong_praise_count": strong_praise_count,
        "active_jobs": len(jobs), "monitored_jobs": coverage_data["monitored_total"],
        "not_monitored": coverage_data["not_monitored"],
    }


def fetch_data_as_of() -> list[dict]:
    from db.state_store import get_all_ingest_cursors
    rows = get_all_ingest_cursors()
    out = []
    for r in rows:
        try:
            when = datetime.fromisoformat(r["cursor_value"]) if r["cursor_value"] else None
        except ValueError:
            when = None
        out.append({"source": r["source"], "as_of": format_ny(when) if when else "(never run)"})
    return sorted(out, key=lambda r: r["source"])


# --- Reconciliation -----------------------------------------------------------

def reconcile(counts: dict, flag_rows: list[dict], watch_data: dict) -> list[tuple]:
    """Returns [(label, db_count, pdf_count, ok)] - asserted before saving."""
    flags_in_pdf = len(flag_rows)
    flags_in_db = sum(counts["flags_by_severity"].values())
    waiting_overdue_in_pdf = len(watch_data["waiting_overdue"])
    waiting_overdue_in_db = counts["watches_by_status"].get("waiting", 0) + counts["watches_by_status"].get("overdue", 0)

    checks = [
        ("Flags (total)", flags_in_db, flags_in_pdf),
        ("Watches (waiting + overdue)", waiting_overdue_in_db, waiting_overdue_in_pdf),
    ]
    return [(label, db_n, pdf_n, db_n == pdf_n) for label, db_n, pdf_n in checks]


# --- PDF building --------------------------------------------------------------

from xml.sax.saxutils import escape as _xml_escape  # noqa: E402

from reportlab.pdfgen import canvas as pdfcanvas  # noqa: E402
from reportlab.platypus import Flowable  # noqa: E402


def clean(text) -> str:
    """Every piece of DB-derived (or otherwise dynamic) text must go
    through this before it is substituted into a Paragraph's mini-XML
    markup: strips em/en dashes (never used anywhere in this report)
    and XML-escapes it so a stray "<" or "&" in real customer text can
    never be mistaken for markup or break the parser. Never call this
    on markup you wrote yourself (e.g. a literal "<br/>") - only on the
    dynamic value being substituted in."""
    text = "" if text is None else str(text)
    return _xml_escape(strip_dashes(text))

_STYLES = getSampleStyleSheet()
STYLE_BODY = ParagraphStyle("body8", parent=_STYLES["Normal"], fontSize=7.5, leading=9, alignment=TA_LEFT)
STYLE_BODY_ITALIC = ParagraphStyle("bodyItalic8", parent=STYLE_BODY, fontName="Helvetica-Oblique")
STYLE_SMALL = ParagraphStyle("small7", parent=STYLE_BODY, fontSize=7, leading=8, textColor=colors.grey)
STYLE_HEAD = ParagraphStyle("head", parent=_STYLES["Heading1"], textColor=NAVY, fontSize=16, spaceAfter=10)
STYLE_SUBHEAD = ParagraphStyle("subhead", parent=_STYLES["Heading2"], textColor=NAVY, fontSize=12, spaceAfter=6)
STYLE_BOX = ParagraphStyle("box", parent=_STYLES["Normal"], fontSize=8.5, leading=11)
STYLE_TABLE_HEADER = ParagraphStyle("tableHeader", parent=STYLE_BODY, fontName="Helvetica-Bold", textColor=colors.white)


class Bookmark(Flowable):
    """Zero-size flowable that adds a PDF outline (bookmark) entry at
    whatever page it lands on."""
    def __init__(self, title: str, key: str, level: int = 0):
        Flowable.__init__(self)
        self.title, self.key, self.level = title, key, level
        self.width = self.height = 0

    def draw(self):
        self.canv.bookmarkPage(self.key)
        self.canv.addOutlineEntry(self.title, self.key, level=self.level, closed=0)


def _make_numbered_canvas(generated_at_str: str):
    class NumberedCanvas(pdfcanvas.Canvas):
        def __init__(self, *args, **kwargs):
            pdfcanvas.Canvas.__init__(self, *args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total_pages = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self._draw_furniture(total_pages, generated_at_str)
                pdfcanvas.Canvas.showPage(self)
            pdfcanvas.Canvas.save(self)

        def _draw_furniture(self, total_pages, generated_at_str):
            self.saveState()
            self.setFont("Helvetica-Bold", 10)
            self.setFillColor(NAVY)
            self.drawString(MARGIN, PAGE_SIZE[1] - 0.3 * inch, HEADER_TEXT)
            self.setStrokeColor(NAVY)
            self.setLineWidth(0.5)
            self.line(MARGIN, PAGE_SIZE[1] - 0.38 * inch, PAGE_SIZE[0] - MARGIN, PAGE_SIZE[1] - 0.38 * inch)

            self.setFont("Helvetica", 7)
            self.setFillColor(colors.grey)
            self.drawString(MARGIN, 0.25 * inch, f"Generated {generated_at_str}. {FOOTER_NOTICE}")
            self.drawRightString(PAGE_SIZE[0] - MARGIN, 0.25 * inch, f"Page {self._pageNumber} of {total_pages}")
            self.restoreState()

    return NumberedCanvas


def _p(text) -> Paragraph:
    if text in (None, ""):
        return Paragraph("-", STYLE_BODY)
    return Paragraph(clean(text), STYLE_BODY)


def _p_lines(*parts) -> Paragraph:
    """Each part is RAW dynamic text (never markup) - cleaned
    individually, then joined with a line break. Use this instead of
    _p() whenever a cell needs more than one line, so the <br/> tags
    this function inserts are never themselves escaped away."""
    cleaned = [clean(p) for p in parts]
    return Paragraph("<br/>".join(cleaned), STYLE_BODY)


def _table(header: list[str], rows: list[list], col_widths: "list | None" = None) -> Table:
    # Safety net: any cell a section builder forgot to wrap in _p()/
    # _p_lines() (a bare string or number) is cleaned here too, so every
    # table cell in the report is guaranteed dash-stripped and escaped.
    safe_rows = [[cell if isinstance(cell, Flowable) else _p(cell) for cell in row] for row in rows]
    data = [[Paragraph(f"<b>{h}</b>", STYLE_TABLE_HEADER) for h in header]] + safe_rows
    t = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _box(title: str, lines: list[str]) -> Table:
    body = "<br/>".join(lines)
    content = Paragraph(f"<b>{title}</b><br/>{body}", STYLE_BOX)
    t = Table([[content]], colWidths=[PAGE_SIZE[0] - 2 * MARGIN])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eff6ff")),
        ("BOX", (0, 0), (-1, -1), 0.6, NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


# --- Section builders -----------------------------------------------------------

def build_section_0(counts: dict, data_as_of: list[dict], since_label: str, generated_at_str: str) -> list:
    flow = [Bookmark("Section 0: Summary", "sec0")]
    flow.append(Paragraph("Casey Review Pack", STYLE_HEAD))
    flow.append(Paragraph(f"Generated {generated_at_str}. Data window: {since_label}.", STYLE_BODY))
    flow.append(Spacer(1, 4))
    flow.append(_table(
        ["Source", "Data as of"],
        [[r["source"], r["as_of"]] for r in data_as_of],
        col_widths=[2.5 * inch, 2.5 * inch],
    ))
    flow.append(Spacer(1, 10))

    sev_rows = [[sev, str(counts["flags_by_severity"].get(sev, 0))] for sev in ("Critical", "High", "Medium", "Low")]
    status_rows = [[st, str(counts["flags_by_status"].get(st, 0))] for st in
                   ("Open", "In Process", "Resolved", "No Action Taken")]
    watch_rows = [[st, str(counts["watches_by_status"].get(st, 0))] for st in ("waiting", "responded", "overdue")]

    flow.append(Paragraph("Flags by severity and status", STYLE_SUBHEAD))
    flow.append(_table(["Severity", "Count"], sev_rows, col_widths=[2 * inch, 1.5 * inch]))
    flow.append(Spacer(1, 6))
    flow.append(_table(["Status", "Count"], status_rows, col_widths=[2 * inch, 1.5 * inch]))
    flow.append(Spacer(1, 10))

    flow.append(Paragraph("Other counts", STYLE_SUBHEAD))
    other_rows = [
        ["Flagged projects", str(counts["flagged_projects"])],
        ["Monitored jobs / Active jobs", f"{counts['monitored_jobs']} / {counts['active_jobs']}"],
        ["Incoming emails analyzed", str(counts["incoming_analyzed"])],
        ["Calls analyzed", str(counts["calls_analyzed"])],
        ["Strong praise count", str(counts["strong_praise_count"])],
    ]
    flow.append(_table(["Metric", "Value"], other_rows, col_widths=[3 * inch, 2 * inch]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Email watches by status", STYLE_SUBHEAD))
    flow.append(_table(["Status", "Count"], watch_rows, col_widths=[2 * inch, 1.5 * inch]))
    flow.append(Spacer(1, 12))

    flow.append(_box("What this report is", [
        "Every item below is an AI suggestion from Casey, not a finding of fact.",
        "Humans decide what happens next: whether a flag is correct, what severity it should be, and how to respond.",
        "Casey never contacts customers. It only reads messages and suggests flags for BCS staff to review.",
    ]))
    flow.append(Spacer(1, 8))
    flow.append(_box("How to review", [
        "For each flag in Section 2: mark Agree or Disagree, write the correct severity if different, and add a note.",
        "For each item in the Section 4 response watch table: mark whether a reply was actually sent, how, and add a note.",
    ]))
    flow.append(PageBreak())
    return flow


def build_section_1() -> list:
    from agents.casey.taxonomy import SEVERITIES, SEVERITY_GUIDE

    flow = [Bookmark("Section 1: How Casey decides", "sec1")]
    flow.append(Paragraph("How Casey decides", STYLE_HEAD))
    flow.append(Paragraph("Severity guide", STYLE_SUBHEAD))
    guide_rows = [[sev, SEVERITY_GUIDE[sev]] for sev in SEVERITIES]
    flow.append(_table(["Severity", "Meaning"], guide_rows, col_widths=[1.3 * inch, 8 * inch]))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Rules in short", STYLE_SUBHEAD))
    for line in (
        "Casey looks at the current message and the project history together, never the message alone.",
        "A polite message does not clear an earlier serious concern, and never lowers an existing flag on its own.",
        "Humans change a flag's status and can override its severity. Casey itself never changes a status.",
        "Casey scores incoming customer words only: incoming email, and the customer's own lines of a call. "
        "It never scores outgoing email or the PM's side of a call.",
    ):
        flow.append(Paragraph(f"- {line}", STYLE_BODY))
        flow.append(Spacer(1, 3))
    flow.append(PageBreak())
    return flow


def build_section_2(flag_rows: list[dict]) -> list:
    flow = [Bookmark("Section 2: Red flags", "sec2")]
    flow.append(Paragraph(f"Red flags ({len(flag_rows)})", STYLE_HEAD))
    header = ["ID", "Severity / Status", "Customer", "Project", "PM / Estimator", "Source, date",
              "Reason / Root cause", "Summary and evidence", "Dates / Related", "Agree? Y/N",
              "Correct severity", "Notes"]
    rows = []
    for f in flag_rows:
        rows.append([
            _p(f["id"]),
            Paragraph(f'{_severity_badge_text(f["severity"])}<br/>{clean(f["status"])}', STYLE_BODY),
            Paragraph(f'{clean(f["customer_name"])}<br/><font color="grey" size="6">{clean(f["customer_email"])}</font>', STYLE_BODY),
            _p_lines(f'{f["job_type"]}, {f["address"]}', f["sheet_tab"]),
            _p_lines(f["pm_name"], f["estimator_name"]),
            _p(f["source_desc"]),
            _p_lines(f["primary_reason"], f'{f["root_cause"]} (suggested)'),
            Paragraph(f'{clean(f["issue_summary"])}<br/><i>"{clean(f["evidence_quote"])}"</i>', STYLE_BODY),
            _p_lines(f'First: {f["first_detected"]}', f'Last: {f["last_updated"]}',
                     f'Related: {f["related_count"]}, Escalated: {f["escalated"]}'),
            _p(""), _p(""), _p(""),
        ])
    flow.append(_table(header, rows, col_widths=[
        0.3 * inch, 0.7 * inch, 1.1 * inch, 1.1 * inch, 0.9 * inch, 1.1 * inch,
        1.1 * inch, 1.6 * inch, 1.1 * inch, 0.55 * inch, 0.7 * inch, 0.8 * inch,
    ]))
    flow.append(PageBreak())
    return flow


def _severity_badge_text(severity: str) -> str:
    # Paragraph.text isn't a real attribute on reportlab's Paragraph; this
    # mirrors _severity_badge()'s markup so section 2's cell can embed it
    # directly as a string rather than nesting a Flowable inside markup.
    color = SEVERITY_COLORS.get(severity, colors.black)
    return f'<font color="{color.hexval()}"><b>{severity.upper()}</b></font>'


def build_section_3(lowest: list[dict], sample: list[dict], praise: list[dict]) -> list:
    flow = [Bookmark("Section 3: Spot check", "sec3")]
    flow.append(Paragraph("Spot check: what Casey did NOT flag", STYLE_HEAD))

    small_header = ["Customer", "Email", "Project", "PM", "Date", "Source", "Sentiment", "Excerpt", "Agree? Y/N", "Notes"]
    col_widths = [1.1 * inch, 1.3 * inch, 1 * inch, 0.8 * inch, 1 * inch, 1.3 * inch, 0.7 * inch, 2.3 * inch, 0.6 * inch, 0.9 * inch]

    def _rows(items):
        return [[
            _p(i["customer_name"]), _p(i["customer_email"]), _p(i["job_type"]), _p(i["pm_name"]),
            _p(i["date"]), _p(i["source_desc"]), _p(round(i["sentiment_score"], 2)), _p(i["excerpt"]), _p(""), _p(""),
        ] for i in items]

    flow.append(Paragraph("(a) 15 lowest-sentiment analyzed messages with no flag", STYLE_SUBHEAD))
    flow.append(_table(small_header, _rows(lowest), col_widths=col_widths))
    flow.append(Spacer(1, 10))

    flow.append(Paragraph(f"(b) Random sample of {len(sample)} analyzed messages with no flag (fixed seed {RANDOM_SAMPLE_SEED})", STYLE_SUBHEAD))
    flow.append(_table(small_header, _rows(sample), col_widths=col_widths))
    flow.append(Spacer(1, 10))

    flow.append(Paragraph("(c) Strong praise items", STYLE_SUBHEAD))
    praise_rows = [[_p(p["quote"]), _p(p["customer_name"]), _p(p["pm_name"]), _p(p["date"])] for p in praise]
    flow.append(_table(["Quote", "Customer", "PM", "Date"], praise_rows,
                        col_widths=[5 * inch, 1.8 * inch, 1.3 * inch, 1.3 * inch]))
    flow.append(PageBreak())
    return flow


def build_section_4(watch_data: dict) -> list:
    flow = [Bookmark("Section 4: Response watch", "sec4")]
    flow.append(Paragraph("Casey, 24h Email Response Watch", STYLE_HEAD))
    flow.append(_box("On what basis an email is marked overdue", [
        "A customer email counts only if it is incoming, not automated, and matched to an active job.",
        "The clock is 24 consecutive calendar hours from the time the email was received (config casey_response_window_hours).",
        "It is answered if, after receipt and inside the window, there is an outgoing email in the same thread to that "
        "customer, or a connected outgoing call to that customer (call longer than 0 seconds, not voicemail, "
        "customer spoke at least 3 words).",
        "A voicemail or unanswered call does not count. Automated mail is excluded. SMS is not included yet.",
        "Casey only sees replies sent from the PM mailbox it monitors and calls on connected Dialpad lines. A reply "
        "sent from another mailbox, another address or another channel is NOT visible, so some overdue items may be "
        "false alerts. Emails that need no reply, for example a short thank you, are also counted.",
    ]))
    flow.append(Spacer(1, 10))

    open_rows = watch_data["waiting_overdue"]
    overdue_rows_only = [r for r in open_rows if r["status"] == "overdue"]

    by_pm: dict[str, int] = {}
    for r in overdue_rows_only:
        by_pm[r["pm_name"]] = by_pm.get(r["pm_name"], 0) + 1
    total_overdue = len(overdue_rows_only) or 1
    pm_table_rows = [[pm, str(n), f"{n / total_overdue * 100:.0f}%"] for pm, n in sorted(by_pm.items(), key=lambda kv: -kv[1])]

    by_bucket: dict[str, int] = {}
    for r in overdue_rows_only:
        bucket = age_bucket(r["hours_overdue"])
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
    bucket_rows = [[b, str(by_bucket.get(b, 0))] for b in ("Under 24h late", "1 to 3 days", "Over 3 days")]

    no_reply_count = sum(1 for r in overdue_rows_only if r["review_hint"] == "Short message, may need no reply")

    flow.append(Paragraph(
        f"Waiting: {len(watch_data['waiting_overdue']) - len(overdue_rows_only)}, "
        f"Overdue: {len(overdue_rows_only)}, Responded (most recent 15 shown): {len(watch_data['responded_recent'])}.",
        STYLE_BODY,
    ))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph("Overdue by PM", STYLE_SUBHEAD))
    flow.append(_table(["PM", "Overdue count", "Share of overdue"], pm_table_rows,
                        col_widths=[2 * inch, 1.5 * inch, 1.5 * inch]))
    flow.append(Spacer(1, 8))
    flow.append(Paragraph("Overdue by age", STYLE_SUBHEAD))
    flow.append(_table(["Age bucket", "Count"], bucket_rows, col_widths=[2 * inch, 1.5 * inch]))
    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        f"Probably no reply needed (heuristic: fewer than {NO_REPLY_WORD_LIMIT} words, or a thanks/acknowledgement "
        f"phrase only, and no question mark): {no_reply_count} of {len(overdue_rows_only)} overdue items.",
        STYLE_BODY,
    ))
    flow.append(PageBreak())

    flow.append(Paragraph("All waiting and overdue items, most overdue first", STYLE_SUBHEAD))
    header = ["Customer / Email", "Project", "PM / Mailbox", "Subject", "Received", "Due",
              "Overdue / Left", "Why flagged", "Review hint", "Reply sent? Y/N", "How", "Notes"]
    rows = [[
        _p_lines(r["customer_name"], r["customer_email"]),
        _p(f'{r["job_type"]} {r["address"]}'.strip()),
        _p_lines(r["pm_name"], r["mailbox_email"]),
        _p(r["subject"]), _p(r["received"]), _p(r["due"]), _p(r["time_display"]),
        _p(r["why_flagged"]), _p(r["review_hint"]), _p(""), _p(""), _p(""),
    ] for r in open_rows]
    flow.append(_table(header, rows, col_widths=[
        1.2 * inch, 1 * inch, 1.1 * inch, 1.1 * inch, 0.9 * inch, 0.9 * inch,
        0.8 * inch, 1.9 * inch, 1 * inch, 0.6 * inch, 0.7 * inch, 0.8 * inch,
    ]))
    flow.append(Spacer(1, 10))

    flow.append(Paragraph("15 most recently responded items", STYLE_SUBHEAD))
    resp_header = ["Customer email", "PM", "Received", "Responded", "Response type", "Hours to respond"]
    resp_rows = [[
        _p(r["customer_email"]), _p(r["pm_name"]), _p(r["received"]), _p(r["responded"]),
        _p(r["response_type"]), _p(r["hours_to_respond"]),
    ] for r in watch_data["responded_recent"]]
    flow.append(_table(resp_header, resp_rows, col_widths=[2 * inch, 1.3 * inch, 1.5 * inch, 1.5 * inch, 1.2 * inch, 1.2 * inch]))
    flow.append(PageBreak())
    return flow


def build_section_5(counts: dict) -> list:
    flow = [Bookmark("Section 5: Known limits", "sec5")]
    flow.append(Paragraph("Known limits and open items", STYLE_HEAD))
    nm = counts["not_monitored"]
    unrecognized_total = sum(e["count"] for e in nm["unrecognized_pm_name"].values())
    flow.append(_box("Coverage", [
        f"Monitored jobs: {counts['monitored_jobs']} of {counts['active_jobs']} active jobs.",
        f"Not monitored: {nm['on_hold']} on comms hold, {nm['blank_pm']} with a blank PM, "
        f"{unrecognized_total} with a PM name not recognized in pm_config, "
        f"{sum(nm['missing_email'].values())} with a PM record missing an email.",
    ]))
    flow.append(Spacer(1, 10))
    flow.append(_box("Other known limits", [
        "SMS messages are not included yet, only email and calls.",
        "A reply sent from outside the monitored PM mailboxes or Dialpad lines is not visible to Casey, so some "
        "overdue response-watch items may be false alerts.",
        "One known calibration difference from the evaluation set: for Julie Harrison's October 9 message, the "
        "model suggests High while the BCS reference label is Medium. Not re-tuned; reported as-is.",
    ]))
    return flow


def write_pdf(out_path: Path, flow: list, generated_at_str: str) -> None:
    doc = BaseDocTemplate(str(out_path), pagesize=PAGE_SIZE,
                           leftMargin=MARGIN, rightMargin=MARGIN, topMargin=0.55 * inch, bottomMargin=0.5 * inch)
    frame = Frame(MARGIN, 0.5 * inch, PAGE_SIZE[0] - 2 * MARGIN, PAGE_SIZE[1] - 1.05 * inch, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
    doc.build(flow, canvasmaker=_make_numbered_canvas(generated_at_str))


def _refresh() -> None:
    """Runs the existing Casey stages first (ingest, analyze,
    process_flags, run_watches), exactly as agents/casey/main.py does.
    This script itself never calls Gmail, Dialpad or Anthropic other
    than through this explicit, opt-in path."""
    from agents.casey.main import run as casey_run
    casey_run(dry_run=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="30d", help="Data window label for the summary page, e.g. '30d'.")
    parser.add_argument("--refresh", action="store_true",
                         help="Run ingest/analyze/process_flags/run_watches first (off by default).")
    args = parser.parse_args()

    if args.refresh:
        print("Refreshing: running Casey stages first (ingest, analyze, process_flags, run_watches)...")
        _refresh()

    print("Reading the local database (read only)...")
    counts = fetch_counts()
    data_as_of = fetch_data_as_of()
    flag_rows = fetch_flags()
    lowest = fetch_not_flagged_lowest_sentiment(15)
    sample = fetch_not_flagged_random_sample(20, RANDOM_SAMPLE_SEED)
    praise = fetch_strong_praise(10)
    watch_data = fetch_response_watches()

    checks = reconcile(counts, flag_rows, watch_data)
    print("\nDB vs PDF reconciliation:")
    print(f"{'Check':<32} {'DB':>8} {'PDF':>8} {'OK':>5}")
    all_ok = True
    for label, db_n, pdf_n, ok in checks:
        print(f"{label:<32} {db_n:>8} {pdf_n:>8} {'yes' if ok else 'NO':>5}")
        all_ok = all_ok and ok
    if not all_ok:
        print("ERROR: reconciliation failed, not writing the PDF.", file=sys.stderr)
        sys.exit(1)

    from utils.timeutil import now_ny
    now = now_ny()
    generated_at_str = now.strftime("%b %d, %Y %I:%M %p")
    out_dir = Path(__file__).parent.parent / "out"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"casey_review_pack_{now.strftime('%Y%m%d_%H%M')}.pdf"

    flow = []
    flow += build_section_0(counts, data_as_of, args.since, generated_at_str)
    flow += build_section_1()
    flow += build_section_2(flag_rows)
    flow += build_section_3(lowest, sample, praise)
    flow += build_section_4(watch_data)
    flow += build_section_5(counts)

    write_pdf(out_path, flow, generated_at_str)

    overdue_rows_only = [r for r in watch_data["waiting_overdue"] if r["status"] == "overdue"]
    hint_counts: dict[str, int] = {}
    for r in overdue_rows_only:
        hint_counts[r["review_hint"]] = hint_counts.get(r["review_hint"], 0) + 1

    size_bytes = out_path.stat().st_size
    print(f"\nWrote {out_path} ({size_bytes} bytes)")
    print(f"Flags in report: {len(flag_rows)}")
    print(f"Section 3: {len(lowest)} lowest-sentiment, {len(sample)} random sample, {len(praise)} strong praise")
    print(f"Section 4: {len(watch_data['waiting_overdue'])} waiting/overdue, "
          f"{len(watch_data['responded_recent'])} recently responded")
    print(f"Overdue items by review hint: {hint_counts}")


if __name__ == "__main__":
    main()
