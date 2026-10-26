"""Builds and delivers OCA's single daily operations checklist.

One message, in two forms (HTML email + plain-text Slack), with sections
in this fixed order (empty ones omitted):
  1. Deadline to Start changed (this run)
  2. Start deadline passed, job still in To Start
  3. Starting soon (within N days)
  4. No PM assigned
  5. PM name not recognized
  6. No readable deadline (To Start)
  7. Footer counts

Each section states its Reason / Action / Clears when ONCE, in the
section header — not repeated on every item line.
"""
import os
from pathlib import Path
from typing import Any

from utils.logger import get_logger
from utils.timeutil import now_ny
from config.loader import cfg

logger = get_logger("oca.checklist")

# Slack truncates PER SECTION, not globally — every non-empty section
# always appears, capped lines end with "+N more, see the email". The
# email always has the full lists.
SLACK_PER_SECTION_CAP = 8

_JOB_TYPE_TRUNCATE_LEN = 60


def _truncate_job_type(job_type: str) -> str:
    job_type = job_type or ""
    if len(job_type) <= _JOB_TYPE_TRUNCATE_LEN:
        return job_type
    return job_type[:_JOB_TYPE_TRUNCATE_LEN - 1] + "…"


def _days_phrase(days_until: int) -> str:
    if days_until == 0:
        return "today"
    if days_until > 0:
        return f"in {days_until} day{'s' if days_until != 1 else ''}"
    n = abs(days_until)
    return f"{n} day{'s' if n != 1 else ''} ago"


# ── Section definitions: (key, title, reason, action, clears_when) ──────
_DEADLINE_CHANGED = (
    "deadline_changed",
    "Deadline to Start changed (this run)",
    "These jobs had their Deadline to Start updated in the sheet during this run.",
    "No action needed — informational, confirms the update was picked up.",
    "Automatically — this section only ever shows this run's changes.",
)
_OVERDUE = (
    "overdue",
    "Start deadline passed, job still in To Start",
    "Deadline to Start has passed and the job is still in To Start.",
    "PM confirms the start or updates the date in Timelines.",
    "The job moves to In Process or the date is updated.",
)
_SOON = (
    "soon",
    "Starting soon",  # warning_days is appended at build time
    "Deadline to Start is coming up soon.",
    "Confirm the job is on track to start on time.",
    "The job moves to In Process, the date is updated, or it passes into the overdue section.",
)
_NO_PM = (
    "no_pm",
    "No PM assigned",
    "No Project Manager is assigned in the sheet.",
    "Assign a PM in the Project Manager column.",
    "A recognized PM name is entered.",
)
_PM_UNRECOGNIZED = (
    "pm_unrecognized",
    "PM name not recognized",
    "The PM name in the sheet doesn't match any configured PM.",
    "Add this person in PM config or fix the sheet.",
    "The name matches a configured PM or is corrected in the sheet.",
)
_NO_DEADLINE = (
    "no_deadline_to_start",
    "No readable deadline (To Start)",
    "Deadline to Start is missing or not in a readable date format.",
    "Enter a valid date in the Dead line to start column.",
    "A parseable date is entered.",
)


def _deadline_change_line(change: dict[str, Any]) -> str:
    pm = change.get("pm_name") or "unassigned"
    job_type = _truncate_job_type(change.get("job_type", ""))
    return (
        f"{change.get('client_name', '')} ({job_type}, PM: {pm}): "
        f"changed from {change.get('old_deadline', '')} to {change.get('new_deadline', '')}"
    )


def _deadline_item_line(item: dict[str, Any]) -> str:
    job_type = _truncate_job_type(item.get("job_type", ""))
    pm = item.get("pm_name") or "unassigned"
    return (
        f"{item.get('client_name', '')} ({job_type}, PM: {pm}): "
        f"deadline {item.get('deadline')} ({_days_phrase(item.get('days_until', 0))})"
    )


def _no_pm_line(item: dict[str, Any]) -> str:
    job_type = _truncate_job_type(item.get("job_type", ""))
    return f"{item.get('client_name', '')} ({job_type})"


def _unrecognized_pm_line(item: dict[str, Any]) -> str:
    clients = ", ".join(item.get("clients", []))
    count = item.get("job_count", 0)
    return f"'{item.get('raw_name', '')}' — {count} job{'s' if count != 1 else ''} ({clients})"


def _no_deadline_line(item: dict[str, Any]) -> str:
    job_type = _truncate_job_type(item.get("job_type", ""))
    pm = item.get("pm_name") or "unassigned"
    return f"{item.get('client_name', '')} ({job_type}, PM: {pm})"


def _build_sections(
    deadline_changes: list[dict[str, Any]],
    deadline_result: dict[str, Any],
    pm_issues: dict[str, Any],
    warning_days: int,
) -> list[dict[str, Any]]:
    """Returns the ordered list of non-empty sections, each with its
    rendered item lines already sorted per spec."""
    sections: list[dict[str, Any]] = []

    if deadline_changes:
        key, title, reason, action, clears = _DEADLINE_CHANGED
        lines = [_deadline_change_line(c) for c in deadline_changes]
        sections.append({"key": key, "title": title, "reason": reason, "action": action, "clears": clears, "lines": lines})

    overdue = sorted(deadline_result.get("overdue", []), key=lambda i: (i["days_until"], i["client_name"]))
    if overdue:
        key, title, reason, action, clears = _OVERDUE
        sections.append({"key": key, "title": title, "reason": reason, "action": action, "clears": clears,
                          "lines": [_deadline_item_line(i) for i in overdue]})

    soon = sorted(deadline_result.get("soon", []), key=lambda i: (i["deadline"], i["client_name"]))
    if soon:
        key, title, reason, action, clears = _SOON
        title = f"{title} (within {warning_days} days)"
        sections.append({"key": key, "title": title, "reason": reason, "action": action, "clears": clears,
                          "lines": [_deadline_item_line(i) for i in soon]})

    blank = sorted(pm_issues.get("blank", []), key=lambda i: i["client_name"])
    if blank:
        key, title, reason, action, clears = _NO_PM
        sections.append({"key": key, "title": title, "reason": reason, "action": action, "clears": clears,
                          "lines": [_no_pm_line(i) for i in blank]})

    unrecognized = sorted(pm_issues.get("unrecognized", []), key=lambda i: i["raw_name"])
    if unrecognized:
        key, title, reason, action, clears = _PM_UNRECOGNIZED
        sections.append({"key": key, "title": title, "reason": reason, "action": action, "clears": clears,
                          "lines": [_unrecognized_pm_line(i) for i in unrecognized]})

    no_deadline = sorted(deadline_result.get("no_deadline_to_start", []), key=lambda i: i["client_name"])
    if no_deadline:
        key, title, reason, action, clears = _NO_DEADLINE
        sections.append({"key": key, "title": title, "reason": reason, "action": action, "clears": clears,
                          "lines": [_no_deadline_line(i) for i in no_deadline]})

    return sections


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
        deadline_result: from check_deadline_to_start() — overdue, soon,
            no_deadline_to_start, no_deadline_in_process_count.
        pm_issues: from classify_pm_issues() — blank, unrecognized.
        counts: footer counts — to_start, in_process, on_hold, retired_this_run.
        warning_days: the oca_deadline_warning_days value used this run.

    Returns:
        {"html": str, "slack_text": str, "is_empty": bool, "section_counts": dict}
    """
    sections = _build_sections(deadline_changes, deadline_result, pm_issues, warning_days)
    section_counts = {s["key"]: len(s["lines"]) for s in sections}
    is_empty = not sections

    footer_line = (
        f"Active jobs — To Start: {counts.get('to_start', 0)}, In Process: {counts.get('in_process', 0)}. "
        f"On hold: {counts.get('on_hold', 0)}. "
        f"Retired this run: {counts.get('retired_this_run', 0)}. "
        f"No readable deadline — To Start: {len(deadline_result.get('no_deadline_to_start', []))}, "
        f"In Process: {deadline_result.get('no_deadline_in_process_count', 0)}."
    )

    if is_empty:
        html = f"<p>OCA: nothing needs attention.</p><p style='color:#6B7280;font-size:12px'>{footer_line}</p>"
        slack_text = f"OCA: nothing needs attention.\n{footer_line}"
        return {"html": html, "slack_text": slack_text, "is_empty": True, "section_counts": section_counts}

    # ── HTML (full lists, Reason/Action/Clears once per section) ───────────
    html_parts = ["<h2>OCA Daily Checklist</h2>"]
    for s in sections:
        html_parts.append(f"<h3>{s['title']}</h3>")
        html_parts.append(
            f"<p style='font-size:13px;color:#374151'>"
            f"<strong>Reason:</strong> {s['reason']}<br>"
            f"<strong>Action:</strong> {s['action']}<br>"
            f"<strong>Clears when:</strong> {s['clears']}</p>"
        )
        html_parts.append("<table border='1' cellpadding='6' cellspacing='0'>")
        for line in s["lines"]:
            html_parts.append(f"<tr><td>{line}</td></tr>")
        html_parts.append("</table>")
    html_parts.append(f"<p style='color:#6B7280;font-size:12px'>{footer_line}</p>")
    html = "\n".join(html_parts)

    # ── Slack (plain text, capped PER SECTION at SLACK_PER_SECTION_CAP) ─────
    slack_parts = ["OCA Daily Checklist"]
    for s in sections:
        slack_parts.append(f"\n{s['title']}:")
        slack_parts.append(f"Reason: {s['reason']} Action: {s['action']} Clears when: {s['clears']}")
        shown = s["lines"][:SLACK_PER_SECTION_CAP]
        for line in shown:
            slack_parts.append(f"- {line}")
        remaining = len(s["lines"]) - len(shown)
        if remaining > 0:
            slack_parts.append(f"+{remaining} more, see the email")
    slack_parts.append(f"\n{footer_line}")
    slack_text = "\n".join(slack_parts)

    return {"html": html, "slack_text": slack_text, "is_empty": False, "section_counts": section_counts}


def _write_dry_run_files(checklist: dict[str, Any]) -> None:
    out_dir = Path("out")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ny().strftime("%Y%m%d_%H%M")
    html_path = out_dir / f"oca_checklist_{stamp}.html"
    txt_path = out_dir / f"oca_checklist_{stamp}.txt"
    html_path.write_text(checklist["html"])
    txt_path.write_text(checklist["slack_text"])
    logger.info(f"OCA_DRY_RUN — wrote {html_path} and {txt_path}; section counts: {checklist['section_counts']}")
    logger.debug(f"OCA_DRY_RUN — email text:\n{checklist['html']}")
    logger.debug(f"OCA_DRY_RUN — slack text:\n{checklist['slack_text']}")


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
    logger.info(f"Delivering checklist — section counts: {checklist['section_counts']}")
    logger.debug(f"Checklist email text:\n{checklist['html']}")
    logger.debug(f"Checklist Slack text:\n{checklist['slack_text']}")

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
