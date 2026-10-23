"""Builds and delivers OCA's single daily operations checklist.

One message, in two forms (HTML email + plain-text Slack), covering:
  1. Deadline to Start changed (this run)
  2. Start deadline passed, job still in To Start
  3. Starting soon (within N days)
  4. No PM assigned
  5. PM name not recognized
  6. Footer counts

Every item line states what happened, what to do, and what clears it.
"""
import os
from pathlib import Path
from typing import Any

from utils.logger import get_logger
from utils.timeutil import now_ny
from config.loader import cfg

logger = get_logger("oca.checklist")

_MAX_SLACK_ITEM_LINES = 25


def _deadline_change_line(change: dict[str, Any]) -> str:
    pm = change.get("pm_name") or "unassigned"
    return (
        f"{change.get('client_name', '')} ({change.get('job_type', '')}, PM: {pm}): "
        f"changed from {change.get('old_deadline', '')} to {change.get('new_deadline', '')}. "
        f"Reason: Deadline to Start was updated in the sheet this run. "
        f"Action: No action needed — informational, confirms the update was picked up. "
        f"Clears when: automatically — this section only ever shows this run's changes."
    )


def _overdue_line(item: dict[str, Any]) -> str:
    days_ago = abs(item.get("days_until", 0))
    return (
        f"{item.get('client_name', '')} ({item.get('job_type', '')}, PM: {item.get('pm_name', 'unassigned')}): "
        f"deadline {item.get('deadline')} ({days_ago} day{'s' if days_ago != 1 else ''} ago). "
        f"Reason: Deadline to Start was {days_ago} day{'s' if days_ago != 1 else ''} ago and the job is still in To Start. "
        f"Action: PM confirms the start or updates the date in Timelines. "
        f"Clears when: the job moves to In Process or the date is updated."
    )


def _soon_line(item: dict[str, Any], warning_days: int) -> str:
    days = item.get("days_until", 0)
    return (
        f"{item.get('client_name', '')} ({item.get('job_type', '')}, PM: {item.get('pm_name', 'unassigned')}): "
        f"deadline {item.get('deadline')} (in {days} day{'s' if days != 1 else ''}). "
        f"Reason: Deadline to Start is within the next {warning_days} days. "
        f"Action: Confirm the job is on track to start on time. "
        f"Clears when: the job moves to In Process, the date is updated, or it passes into the overdue section."
    )


def _blank_pm_line(item: dict[str, Any]) -> str:
    return (
        f"{item.get('client_name', '')} ({item.get('job_type', '')}). "
        f"Reason: No Project Manager is assigned in the sheet. "
        f"Action: Assign a PM in the Project Manager column. "
        f"Clears when: a recognized PM name is entered."
    )


def _unrecognized_pm_line(item: dict[str, Any]) -> str:
    clients = ", ".join(item.get("clients", []))
    count = item.get("job_count", 0)
    return (
        f"'{item.get('raw_name', '')}' — {count} job{'s' if count != 1 else ''} ({clients}). "
        f"Reason: PM name is not recognized. "
        f"Action: Add this person in PM config or fix the sheet. "
        f"Clears when: the name matches a configured PM or is corrected in the sheet."
    )


def build_checklist(
    deadline_changes: list[dict[str, Any]],
    deadline_result: dict[str, Any],
    pm_issues: dict[str, Any],
    counts: dict[str, Any],
    warning_days: int,
) -> dict[str, Any]:
    """Build the HTML email body and the plain-text Slack message.

    Args:
        deadline_changes: from _sync_all()'s summary.
        deadline_result: from check_deadline_to_start() — {"overdue", "soon", "no_deadline_count"}.
        pm_issues: from classify_pm_issues() — {"blank", "unrecognized"}.
        counts: footer counts — {"to_start", "in_process", "on_hold", "retired_this_run"}.
        warning_days: the oca_deadline_warning_days value used this run (for section 3's wording).

    Returns:
        {"html": str, "slack_text": str, "is_empty": bool}
    """
    sections: list[tuple[str, list[str]]] = []

    if deadline_changes:
        sections.append(("Deadline to Start changed (this run)", [_deadline_change_line(c) for c in deadline_changes]))

    overdue = deadline_result.get("overdue", [])
    if overdue:
        sections.append(("Start deadline passed, job still in To Start", [_overdue_line(i) for i in overdue]))

    soon = deadline_result.get("soon", [])
    if soon:
        sections.append((f"Starting soon (within {warning_days} days)", [_soon_line(i, warning_days) for i in soon]))

    blank = pm_issues.get("blank", [])
    if blank:
        sections.append(("No PM assigned", [_blank_pm_line(i) for i in blank]))

    unrecognized = pm_issues.get("unrecognized", [])
    if unrecognized:
        sections.append(("PM name not recognized", [_unrecognized_pm_line(i) for i in unrecognized]))

    is_empty = not sections

    footer_line = (
        f"Active jobs — To Start: {counts.get('to_start', 0)}, In Process: {counts.get('in_process', 0)}. "
        f"On hold: {counts.get('on_hold', 0)}. "
        f"Retired this run: {counts.get('retired_this_run', 0)}. "
        f"No readable deadline: {deadline_result.get('no_deadline_count', 0)}."
    )

    if is_empty:
        html = f"<p>OCA: nothing needs attention.</p><p style='color:#6B7280;font-size:12px'>{footer_line}</p>"
        slack_text = f"OCA: nothing needs attention.\n{footer_line}"
        return {"html": html, "slack_text": slack_text, "is_empty": True}

    # ── HTML (full, untrimmed) ──────────────────────────────────────────────
    html_parts = ["<h2>OCA Daily Checklist</h2>"]
    for title, lines in sections:
        html_parts.append(f"<h3>{title}</h3><table border='1' cellpadding='6' cellspacing='0'>")
        for line in lines:
            html_parts.append(f"<tr><td>{line}</td></tr>")
        html_parts.append("</table>")
    html_parts.append(f"<p style='color:#6B7280;font-size:12px'>{footer_line}</p>")
    html = "\n".join(html_parts)

    # ── Slack (plain text, trimmed to _MAX_SLACK_ITEM_LINES total item lines) ──
    slack_parts = ["OCA Daily Checklist"]
    remaining = _MAX_SLACK_ITEM_LINES
    total_lines = sum(len(lines) for _, lines in sections)
    shown = 0
    for title, lines in sections:
        if remaining <= 0:
            break
        slack_parts.append(f"\n{title}:")
        for line in lines:
            if remaining <= 0:
                break
            slack_parts.append(f"- {line}")
            remaining -= 1
            shown += 1
    if shown < total_lines:
        slack_parts.append(f"\n+{total_lines - shown} more")
    slack_parts.append(f"\n{footer_line}")
    slack_text = "\n".join(slack_parts)

    return {"html": html, "slack_text": slack_text, "is_empty": False}


def _write_dry_run_files(checklist: dict[str, Any]) -> None:
    out_dir = Path("out")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ny().strftime("%Y%m%d_%H%M")
    html_path = out_dir / f"oca_checklist_{stamp}.html"
    txt_path = out_dir / f"oca_checklist_{stamp}.txt"
    html_path.write_text(checklist["html"])
    txt_path.write_text(checklist["slack_text"])
    logger.info(f"OCA_DRY_RUN — wrote {html_path} and {txt_path}")
    logger.info(f"OCA_DRY_RUN — email text:\n{checklist['html']}")
    logger.info(f"OCA_DRY_RUN — slack text:\n{checklist['slack_text']}")


def send_daily_checklist(checklist: dict[str, Any]) -> None:
    """Deliver the checklist: email to chris_notification_email, Slack to
    the urgentmatters channel. Respects silent_mode exactly like every
    other send in this codebase (the send helpers themselves check it).
    OCA_DRY_RUN=1 builds and logs/writes the checklist but sends nothing.
    """
    if os.getenv("OCA_DRY_RUN", "").strip() == "1":
        _write_dry_run_files(checklist)
        return

    subject = "OCA Daily Checklist" if not checklist["is_empty"] else "OCA: nothing needs attention"

    email_to = cfg("chris_notification_email") or ""
    email_from = cfg("notification_sender_email") or ""
    if email_to and email_from:
        from integrations.gmail import send_email
        try:
            send_email(sender_email=email_from, to_email=email_to, subject=subject, body_html=checklist["html"])
        except Exception as e:
            logger.error(f"send_daily_checklist: email failed: {e}")
    else:
        logger.warning("send_daily_checklist: chris_notification_email/notification_sender_email not configured — skipping email")

    from integrations.slack import send_message
    urgentmatters = cfg("slack_urgentmatters_channel") or "urgentmatters"
    try:
        send_message(urgentmatters, checklist["slack_text"])
    except Exception as e:
        logger.error(f"send_daily_checklist: Slack failed: {e}")
