import io
import os
from datetime import date, datetime
from typing import Any

from utils.logger import get_logger

logger = get_logger("integrations.pdf_report")

_URGENCY_COLORS_RGB = {
    "urgent":  (0.98, 0.88, 0.88),
    "warning": (0.99, 0.97, 0.80),
    "info":    (0.85, 0.92, 0.99),
}

_FLAG_TYPE_LABELS = {
    "stale_record":         "Stale Record",
    "missing_pm":           "Missing PM",
    "unconfirmed_crew":     "Unconfirmed Crew",
    "dropped_invoice":      "Dropped Invoice",
    "readiness_sync":       "HubSpot Sync Issue",
    "deal_not_found":       "Deal Not Found",
    "approaching_deadline": "Approaching Deadline",
    "escalation":           "Escalation",
}


def generate_pdf_report(
    agent: str,
    flags: list[dict[str, Any]],
    run_summary: str = "",
    run_duration: str = "",
) -> bytes:
    """Generate a PDF summary of flags/escalations. Returns PDF bytes, or b'' on error."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib import colors as rl_colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
    except ImportError as e:
        logger.error(f"reportlab not installed — cannot generate PDF report: {e}")
        return b""

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    today_str     = date.today().strftime("%B %d, %Y")
    urgent_count  = sum(1 for f in flags if f.get("urgency") == "urgent")
    warning_count = sum(1 for f in flags if f.get("urgency") == "warning")
    info_count    = sum(1 for f in flags if f.get("urgency", "info") == "info")

    title_style = ParagraphStyle("RTitle", parent=styles["Title"],  fontSize=16, spaceAfter=4,  alignment=TA_LEFT)
    meta_style  = ParagraphStyle("RMeta",  parent=styles["Normal"], fontSize=10, spaceAfter=3)
    foot_style  = ParagraphStyle("RFoot",  parent=styles["Normal"], fontSize=8,
                                 textColor=rl_colors.HexColor("#6B7280"), alignment=TA_CENTER)

    story.append(Paragraph("Blue Collar Scholars — Agent Run Report", title_style))
    story.append(Paragraph(f"Agent: {agent}", meta_style))
    story.append(Paragraph(f"Run date: {today_str}", meta_style))
    if run_duration:
        story.append(Paragraph(f"Run duration: {run_duration}", meta_style))
    story.append(Paragraph(
        f"Total issues: {len(flags)} | Urgent: {urgent_count} | "
        f"Warnings: {warning_count} | Info: {info_count}",
        meta_style,
    ))
    if run_summary:
        story.append(Paragraph(f"Summary: {run_summary}", meta_style))
    story.append(Spacer(1, 0.25 * inch))

    if flags:
        header = ["Customer", "PM", "Issue Type", "Urgency", "Last Contact", "Details"]
        rows = [header]
        style_cmds: list = [
            ("BACKGROUND",    (0, 0), (-1, 0),  rl_colors.HexColor("#1E3A5F")),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  rl_colors.white),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("GRID",          (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#D1D5DB")),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            ("WORDWRAP",      (0, 0), (-1, -1), True),
        ]

        details_style = ParagraphStyle(
            "DetailCell",
            parent=styles["Normal"],
            fontSize=8,
            leading=10,
        )

        for i, flag in enumerate(flags, start=1):
            urgency = flag.get("urgency", "info")
            rgb = _URGENCY_COLORS_RGB.get(urgency, _URGENCY_COLORS_RGB["info"])
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), rl_colors.Color(*rgb)))

            flag_type = flag.get("flag_type", "")
            label = _FLAG_TYPE_LABELS.get(flag_type, flag_type.replace("_", " ").title())

            details = flag.get("details", "")
            deal_id = flag.get("hubspot_deal_id")
            if deal_id:
                details = f"{details} [HubSpot deal/{deal_id}]"

            rows.append([
                (flag.get("client_name") or "")[:40],
                (flag.get("pm_name")     or "")[:25],
                label,
                urgency.title(),
                (flag.get("last_contact_date") or "—")[:12],
                Paragraph(details, details_style),
            ])

        # Customer=85, PM=90, Issue Type=70, Urgency=42, Last Contact=52, Details=165 → total=504pt
        col_widths = [85, 90, 70, 42, 52, 165]
        table = Table(rows, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle(style_cmds))
        story.append(table)
    else:
        story.append(Paragraph("No issues detected this run.", meta_style))

    story.append(Spacer(1, 0.25 * inch))
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated by BCS AI Agents — Confidential — {generated_at}", foot_style))

    doc.build(story)
    return buf.getvalue()


def send_pdf_report(
    agent: str,
    pdf_bytes: bytes,
    slack_channel: str,
    slack_caption: str,
    email_to: str,
    email_from: str,
    email_subject: str,
) -> None:
    """Write PDF to /tmp, upload to Slack, email to recipient, then delete the temp file.

    Respects silent_mode — logs intent but sends nothing when enabled.
    """
    from db.state_store import get_config

    silent = get_config("silent_mode") == "true"
    today_str = date.today().isoformat()
    tmp_path = f"/tmp/bcs_report_{agent}_{today_str}.pdf"

    try:
        with open(tmp_path, "wb") as fh:
            fh.write(pdf_bytes)

        if silent:
            logger.info(f"SILENT MODE — would have uploaded PDF to #{slack_channel}: {slack_caption}")
            logger.info(f"SILENT MODE — would have emailed PDF to {email_to}: {email_subject}")
            return

        # ── Slack file upload ────────────────────────────────────────────────
        from integrations.slack import upload_file
        upload_file(
            channel=slack_channel,
            content=pdf_bytes,
            filename=f"bcs_{agent.lower()}_report_{today_str}.pdf",
            initial_comment=slack_caption,
        )

        # ── Gmail attachment ─────────────────────────────────────────────────
        if not email_from:
            logger.warning("send_pdf_report: notification_sender_email not configured — skipping email")
        elif not email_to:
            logger.warning("send_pdf_report: chris_notification_email not configured — skipping email")
        else:
            from integrations.gmail import send_email_with_attachment
            send_email_with_attachment(
                sender_email=email_from,
                to_email=email_to,
                subject=email_subject,
                body_text=f"Please find the {agent} run report attached.",
                attachment_bytes=pdf_bytes,
                attachment_filename=f"bcs_{agent.lower()}_report_{today_str}.pdf",
            )

    except Exception as e:
        logger.error(f"send_pdf_report({agent}): {e}")
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
