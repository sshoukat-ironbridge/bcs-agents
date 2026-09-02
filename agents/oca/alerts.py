import os
from typing import Any

from utils.logger import get_logger
from config.loader import cfg
from integrations.slack import send_message

logger = get_logger("oca.alerts")


def normalize_pm_name(pm_name: str) -> str:
    """Normalize PM name for consistent matching; handles casing variants like FERNANDA."""
    name = pm_name.strip().title()
    if name.upper() == "FERNANDA":
        return "Fernanda"
    return name


_FLAG_TYPE_LABELS = {
    "stale_record":         "Stale Job Record",
    "missing_pm":           "Missing PM Assignment",
    "unconfirmed_crew":     "Unconfirmed Crew/Sub",
    "dropped_invoice":      "Dropped Invoice Follow-up",
    "readiness_sync":       "HubSpot Sync Issue",
    "deal_not_found":       "Deal Not Found in HubSpot",
    "approaching_deadline": "Approaching 7-Day Deadline",
}

_URGENCY_EMOJI = {
    "urgent":  "🔴",
    "warning": "🟡",
    "info":    "🔵",
}

_URGENCY_RANK = {
    "urgent":  3,
    "warning": 2,
    "info":    1,
}


def _highest_urgency(job_flags: list[dict[str, Any]]) -> str:
    return max(
        (f.get("urgency", "info") for f in job_flags),
        key=lambda u: _URGENCY_RANK.get(u, 0),
        default="info",
    )


def _hubspot_deal_url(deal_id: str) -> str:
    portal_id = cfg("hubspot_portal_id") or "51566851"
    return f"https://app.hubspot.com/contacts/{portal_id}/deal/{deal_id}"


def _get_urgentmatters_channel() -> str:
    return cfg("slack_urgentmatters_channel") or "urgentmatters"


def _route(message: str) -> None:
    send_message(_get_urgentmatters_channel(), message)


def build_alert_message(flag: dict[str, Any]) -> str:
    flag_type = flag.get("flag_type", "")
    label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
    deal_id = flag.get("hubspot_deal_id")
    deal_url = _hubspot_deal_url(deal_id) if deal_id else "—"

    lines = [
        f"🚨 *OCA Alert — {label}*",
        f"Customer: {flag.get('client_name', 'Unknown')}",
        f"PM: {flag.get('pm_name', 'Unassigned')}",
        f"Details: {flag.get('details', '')}",
    ]
    last_contact = flag.get("last_contact_date")
    if last_contact:
        lines.append(f"Last contact: {last_contact}")
    lines.append(f"HubSpot: {deal_url}")
    return "\n".join(lines)


def route_alert(flag: dict[str, Any]) -> None:
    _route(build_alert_message(flag))


def build_combined_alert_message(job_flags: list[dict[str, Any]]) -> str:
    client_name = job_flags[0].get("client_name", "Unknown")
    pm_name = job_flags[0].get("pm_name", "Unassigned")

    lines = [
        f"🚨 *Multiple Issues — {client_name}*",
        f"PM: {pm_name}",
        "",
    ]
    for flag in job_flags:
        flag_type = flag.get("flag_type", "")
        label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
        urgency = flag.get("urgency", "info")
        emoji = _URGENCY_EMOJI.get(urgency, "🔵")
        lines.append(f"{emoji} {label}: {flag.get('details', '')}")

    overall = _highest_urgency(job_flags)
    lines.append("")
    lines.append(f"Overall urgency: {overall}")

    if overall == "urgent":
        deal_id = job_flags[0].get("hubspot_deal_id")
        if deal_id:
            lines.append(f"🔗 HubSpot: {_hubspot_deal_url(deal_id)}")

    return "\n".join(lines)


def route_combined_alert(job_flags: list[dict[str, Any]]) -> None:
    _route(build_combined_alert_message(job_flags))


def build_unresolved_warning_message(flag: dict[str, Any], hours_open: int) -> str:
    flag_type = flag.get("flag_type", "")
    label = _FLAG_TYPE_LABELS.get(flag_type, flag_type)
    client_name = flag.get("client_name", "Unknown")
    pm_name = flag.get("pm_name", "Unassigned")
    return (
        f"⚠️ Unresolved Warning — {label} for {client_name} has been open for "
        f"{hours_open} hours with no resolution. PM: {pm_name}."
    )


def escalate_unresolved_warning(flag: dict[str, Any], hours_open: int) -> None:
    _route(build_unresolved_warning_message(flag, hours_open))


def build_deal_not_found_message(flag: dict[str, Any]) -> str:
    return (
        f"⚠️ *Deal Not Found in HubSpot*\n"
        f"Customer: {flag.get('client_name', 'Unknown')}\n"
        f"PM: {flag.get('pm_name', 'Unassigned')}\n"
        f"Reason: Job exists in sheet but no matching HubSpot deal found\n"
        f"Sheet email: {flag.get('customer_email') or 'Unknown'}\n"
        f"Action: Update customer email in sheet to match HubSpot contact, then re-run OCA"
    )


def send_deal_not_found_alert(flag: dict[str, Any]) -> None:
    _route(build_deal_not_found_message(flag))


def build_no_email_history_message(
    client_name: str,
    pm_name: str,
    pm_email: str,
    customer_email: str,
) -> str:
    return (
        f"⚠️ *No Email History Found*\n"
        f"Customer: {client_name}\n"
        f"PM: {pm_name}\n"
        f"Reason: No emails found in {pm_email} sent folder to {customer_email}\n"
        f"Action: Verify PM has contacted this customer"
    )


def send_no_email_history_alert(
    client_name: str,
    pm_name: str,
    pm_email: str,
    customer_email: str,
) -> None:
    _route(build_no_email_history_message(client_name, pm_name, pm_email, customer_email))


def build_deadline_change_message(
    client_name: str,
    pm_name: str,
    old_deadline: str,
    new_deadline: str,
    deal_id: "str | None",
) -> str:
    deal_url = _hubspot_deal_url(deal_id) if deal_id else "—"
    return (
        f"📅 *Deadline to Start Changed*\n"
        f"Customer: {client_name}\n"
        f"PM: {pm_name or 'Unassigned'}\n"
        f"Previous deadline: {old_deadline}\n"
        f"New deadline: {new_deadline}\n"
        f"HubSpot: {deal_url}"
    )


def send_deadline_change_alert(
    client_name: str,
    pm_name: str,
    old_deadline: str,
    new_deadline: str,
    deal_id: "str | None",
) -> None:
    _route(build_deadline_change_message(client_name, pm_name, old_deadline, new_deadline, deal_id))


def _generate_cadence_pdf(flagged_jobs: list[dict[str, Any]], today_str: str) -> bytes:
    """Generate a PDF for the daily cadence compliance report. Returns b'' on error."""
    try:
        import io
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib import colors as rl_colors
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
    except ImportError as e:
        logger.error(f"reportlab not installed — cannot generate cadence PDF: {e}")
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

    n = len(flagged_jobs)
    title_style = ParagraphStyle("CTitle", parent=styles["Title"],  fontSize=16, spaceAfter=4,  alignment=TA_LEFT)
    meta_style  = ParagraphStyle("CMeta",  parent=styles["Normal"], fontSize=10, spaceAfter=3)
    foot_style  = ParagraphStyle("CFoot",  parent=styles["Normal"], fontSize=8,
                                 textColor=rl_colors.HexColor("#6B7280"), alignment=TA_CENTER)

    story.append(Paragraph("Blue Collar Scholars — Daily Cadence Compliance Report", title_style))
    story.append(Paragraph(f"Report date: {today_str}", meta_style))
    story.append(Paragraph(f"In Process jobs flagged: {n}", meta_style))
    story.append(Paragraph("Standard: PM must contact every In Process customer daily.", meta_style))
    story.append(Spacer(1, 0.25 * inch))

    header = ["Client", "PM", "Last Contact", "Action"]
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
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.HexColor("#F9FAFB"), rl_colors.white]),
    ]

    action_style = ParagraphStyle("CAction", parent=styles["Normal"], fontSize=8, leading=10)
    for job in flagged_jobs:
        rows.append([
            (job.get("client_name") or "")[:40],
            (job.get("pm_name") or "Unassigned")[:25],
            job.get("last_contact_date") or "No contact found",
            Paragraph("PM should contact today per daily cadence standard", action_style),
        ])

    # Client=120, PM=100, Last Contact=80, Action=204 → total=504pt (~letter minus margins)
    col_widths = [120, 100, 80, 204]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))
    story.append(table)

    story.append(Spacer(1, 0.25 * inch))
    from datetime import datetime
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated by BCS AI Agents — Confidential — {generated_at}", foot_style))

    doc.build(story)
    return buf.getvalue()


def send_daily_cadence_report(flagged_jobs: list[dict[str, Any]]) -> None:
    """Email a PDF daily cadence compliance report to chris_notification_email.

    Only sent when there are flagged jobs. Respects silent_mode — skips PDF
    generation and logs intent when enabled. Does not post to Slack.
    """
    from datetime import date
    if not flagged_jobs:
        return

    from db.state_store import get_config
    silent = get_config("silent_mode") == "true"

    today_str = date.today().isoformat()
    n = len(flagged_jobs)
    subject = f"Daily Cadence Compliance Report — {today_str} — {n} job{'s' if n != 1 else ''} flagged"

    email_to   = cfg("chris_notification_email") or ""
    email_from = cfg("notification_sender_email") or ""

    if silent:
        logger.info(f"SILENT MODE — would have emailed PDF cadence report to {email_to}: {subject}")
        return

    if not email_to:
        logger.warning("send_daily_cadence_report: chris_notification_email not configured — skipping")
        return
    if not email_from:
        logger.warning("send_daily_cadence_report: notification_sender_email not configured — skipping")
        return

    pdf_bytes = _generate_cadence_pdf(flagged_jobs, today_str)
    if not pdf_bytes:
        logger.error("send_daily_cadence_report: PDF generation failed — skipping email")
        return

    try:
        from integrations.gmail import send_email_with_attachment
        ok = send_email_with_attachment(
            sender_email=email_from,
            to_email=email_to,
            subject=subject,
            body_text="Please find today's daily cadence compliance report attached.",
            attachment_bytes=pdf_bytes,
            attachment_filename=f"bcs_daily_cadence_{today_str}.pdf",
        )
        if not ok:
            logger.warning(f"send_daily_cadence_report: email send failed for {today_str}")
    except Exception as e:
        logger.error(f"send_daily_cadence_report: {e}")


def send_approaching_deadline_notifications(approaching_jobs: list[dict[str, Any]]) -> None:
    """Send one combined Slack message to #urgentmatters and one email to Chris for jobs at day 6."""
    if not approaching_jobs:
        return

    lines = ["⏰ *Approaching 7-Day PM Contact Deadline — Action Required*", ""]
    for job in approaching_jobs:
        client_name = job.get("client_name", "Unknown")
        pm_name = job.get("pm_name", "Unassigned")
        days = job.get("days_since_contact", 6)
        lines.append(f"• {client_name} (PM: {pm_name}) — {days} days since last contact")
    lines.append("")
    lines.append("These jobs will trigger Casey's backstop email tomorrow if the PM makes no contact today.")

    send_message(_get_urgentmatters_channel(), "\n".join(lines))

    chris_email = cfg("chris_notification_email") or ""
    sender_email = cfg("notification_sender_email") or ""
    if not chris_email:
        return
    if not sender_email:
        logger.warning("send_approaching_deadline_notifications: notification_sender_email not configured — skipping Chris email")
        return

    try:
        from integrations.gmail import send_email
        count = len(approaching_jobs)
        subject = f"Approaching Deadline — {count} job{'s' if count != 1 else ''} at day 6 of PM contact gap"
        body_html = "<br>".join(line.replace("*", "<b>", 1).replace("*", "</b>", 1) if "*" in line else line for line in lines)
        send_email(sender_email=sender_email, to_email=chris_email, subject=subject, body_html=f"<p>{body_html}</p>")
    except Exception as e:
        logger.warning(f"send_approaching_deadline_notifications: failed to email Chris: {e}")
