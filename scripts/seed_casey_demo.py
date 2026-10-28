"""Seeds (or resets) the Casey dashboard's demo data set: realistic,
clearly-fictional customers, flags, sentiment history, praise, and
response watches, all marked is_demo=True. Never touches a real
(is_demo=False) row.

Usage:
    docker compose run --rm oca python scripts/seed_casey_demo.py           # seed once
    docker compose run --rm oca python scripts/seed_casey_demo.py --reset  # delete demo rows, then reseed

Real PM names come from pm_config; real estimator names come from the
distinct estimator_name values already in casey_active_jobs (falls back
to a short invented list if that table is empty, e.g. a brand-new
environment that hasn't synced jobs yet).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("casey.seed_demo")

CALIBRATION_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "casey" / "calibration.json"

FALLBACK_ESTIMATORS = ["Jordan Reyes", "Sam Whitfield", "Taylor Brooks"]

# --- Invented customers' quotes (clearly fictional; never run through the
# real model — these severities/sentiments are authored directly) --------

INVENTED_MESSAGES = {
    "Marcus Webb": [
        ("One of the crew members was pretty rude to my wife this morning — made a comment "
         "about the mess we hadn't cleaned up ourselves. Not okay.", "negative", -0.6),
    ],
    "Priya Natarajan": [
        ("The tile work in the bathroom is uneven in two spots and one of the grout lines is "
         "already cracking. This was supposed to be finished work.", "negative", -0.55),
    ],
    "Tom Reyes": [
        ("We were told crews would be here Monday. It's Thursday and nobody has shown up or "
         "called. We need an actual date.", "negative", -0.6),
    ],
    "Sandra Kim": [
        ("I've emailed twice this week and called once and haven't heard back from anyone on "
         "our project. Starting to feel ignored.", "negative", -0.45),
        ("Thanks for calling me back today, appreciate you taking the time to walk through it.", "positive", 0.4),
    ],
    "Walter Boyce": [
        ("The final invoice is about $1,800 more than the estimate and nobody explained why "
         "before sending it.", "negative", -0.4),
    ],
    "Renee Castillo": [
        ("The crew left debris and food wrappers in the driveway again after Tuesday. This is "
         "the second time.", "negative", -0.35),
    ],
    "Omar Haddad": [
        ("The cabinet hardware that showed up is the wrong finish — we ordered brushed nickel "
         "and got brass.", "negative", -0.4),
    ],
    "Lindsey Pruitt": [
        ("I'm confused about what's actually included in the change order — the drawing shows "
         "something different from what we discussed on the call.", "negative", -0.4),
        ("Nobody has given us an update in about a week and a half now.", "negative", -0.3),
    ],
    "Derek Voss": [
        ("The punch list from three weeks ago still has two items open — the closet door and "
         "the hallway outlet.", "negative", -0.35),
        ("Still waiting to hear back about the warranty claim on the deck railing.", "negative", -0.3),
    ],
    "Angela Pham": [
        ("Our neighbor mentioned the permit sign hasn't been posted — is that something we "
         "need to worry about?", "negative", -0.25),
        ("The side fence got dented when the equipment was brought in last week.", "negative", -0.35),
    ],
    "Nina Albrecht": [
        ("There's a $220 charge on the invoice I don't recognize — can someone explain it?", "negative", -0.3),
        ("We were told the crew would start the week of the 14th and it's now a week later "
         "with no word.", "negative", -0.35),
    ],
    "Felicia Grant": [
        ("There's an exposed wire near the water heater that's been like that since Friday — "
         "wanted to flag it in case it's a hazard.", "negative", -0.4),
        ("We just got the change order invoice and the number is quite a bit higher than the "
         "estimate we approved — would like to understand the difference.", "negative", -0.35),
    ],
}

# (customer, severity, status, primary_reason, root_cause, created_days_ago,
#  resolution | None, escalation_from | None, improving_event | bool)
INVENTED_FLAGS = [
    ("Marcus Webb", "High", "Open", "Crew professionalism / conduct",
     "In-house crew performance", 10, None, None, False),
    ("Marcus Webb", "Low", "Open", "Rework required",
     "Undetermined (default)", 11, None, None, False),
    ("Priya Natarajan", "High", "In Process", "Workmanship / quality of work",
     "Subcontractor performance", 18, None, "Medium", False),
    ("Tom Reyes", "High", "Open", "Schedule delay / missed dates",
     "Scheduling / capacity constraint", 5, None, None, False),
    ("Sandra Kim", "Medium", "Open", "Unresponsive PM or estimator",
     "Internal communication breakdown", 12, None, None, True),
    ("Walter Boyce", "Medium", "Resolved", "Estimate vs. final invoice gap",
     "Estimating error / scope gap", 40, ("Invoice corrected, customer satisfied", 30), None, False),
    ("Renee Castillo", "Medium", "Open", "Cleanliness / site condition",
     "Standard exists / not followed", 20, None, None, False),
    ("Omar Haddad", "Medium", "In Process", "Materials defect / wrong materials",
     "Supplier / material delay", 8, None, None, False),
    ("Lindsey Pruitt", "Medium", "Open", "Scope / change order confusion",
     "Estimating error / scope gap", 15, None, None, False),
    ("Lindsey Pruitt", "Low", "Open", "Lack of communication / no updates",
     "Internal communication breakdown", 6, None, None, False),
    ("Derek Voss", "Medium", "No Action Taken", "Punch list not completed",
     "Process gap / no standard exists", 35, ("Customer withdrew complaint after follow-up call", 28), None, False),
    ("Derek Voss", "Low", "Open", "Warranty or rework response time",
     "Undetermined (default)", 4, None, None, False),
    ("Angela Pham", "Low", "Open", "Permitting / inspection issue",
     "Documentation / paperwork error", 9, None, None, False),
    ("Angela Pham", "Low", "Resolved", "Property or landscape damage",
     "In-house crew performance", 25, ("Landscaping repaired at no cost", 20), None, False),
    ("Nina Albrecht", "Low", "Open", "Pricing or billing dispute",
     "Documentation / paperwork error", 3, None, None, False),
    ("Nina Albrecht", "Low", "In Process", "Job not started on time",
     "Scheduling / capacity constraint", 7, None, None, False),
    ("Felicia Grant", "Low", "Open", "Safety concern",
     "Undetermined (default)", 2, None, None, False),
    ("Felicia Grant", "Medium", "Open", "Unexpected charges",
     "Estimating error / scope gap", 14, None, None, False),
]

STRONG_PRAISE = [
    ("Harold Jennings", "The whole crew was fantastic from day one — on time, careful with our "
     "things, and the finished kitchen is better than we pictured. We couldn't be happier."),
    ("Dana Whitfield", "I have to say this was the smoothest home project we've ever done. Every "
     "question got answered same day. Genuinely impressed."),
    ("Carlos Pena", "Please pass this along to the crew — the new deck is beautiful and they "
     "treated our yard like it was their own. Five stars, easily."),
    ("Mei Lin Zhou", "Honestly one of the best contractor experiences we've had. Communication "
     "was excellent the entire time and the work speaks for itself."),
    ("Bridget O'Connell", "We are so happy with how the bathroom turned out. The team went above "
     "and beyond and we will absolutely recommend BCS to our friends."),
    ("Antoine Fields", "Just wanted to say thank you — everyone who came through our house was "
     "professional and the finished work is exactly what we hoped for."),
    ("Rosalind Marsh", "The project manager kept us in the loop every step and the final result "
     "exceeded what we expected. Thank you doesn't feel like enough."),
    ("Keisha Thornton", "This was a big project and it went better than we could have imagined. "
     "Truly grateful for the care everyone put into it."),
]

# (customer, status, received_hours_ago, due_offset_hours, responded_hours_after | None)
RESPONSE_WATCHES = [
    ("Watch Customer A", "responded", 60, 24, 4),
    ("Watch Customer B", "responded", 55, 24, 10),
    ("Watch Customer C", "responded", 50, 24, 2),
    ("Watch Customer D", "responded", 45, 24, 18),
    ("Watch Customer E", "responded", 40, 24, 6),
    ("Watch Customer F", "responded", 36, 24, 12),
    ("Watch Customer G", "waiting", 2, 24, None),
    ("Watch Customer H", "waiting", 4, 24, None),
    ("Watch Customer I", "waiting", 6, 24, None),
    ("Watch Customer J", "waiting", 8, 24, None),
    ("Watch Customer K", "overdue", 30, 24, None),
    ("Watch Customer L", "overdue", 34, 24, None),
    ("Watch Customer M", "overdue", 40, 24, None),
    ("Watch Customer N", "overdue", 48, 24, None),
]


def _load_calibration_quotes() -> dict:
    cases = json.loads(CALIBRATION_PATH.read_text())
    by_customer: dict[str, list] = {}
    for case in cases:
        if not case.get("date"):
            continue  # the Sandstrom pre-cancellation summary isn't a verbatim message
        by_customer.setdefault(case["customer"], []).append(case)
    for cases_for_customer in by_customer.values():
        cases_for_customer.sort(key=lambda c: c["date"])
    return by_customer


def _severity_to_sentiment(severity: str) -> "tuple[str, float]":
    return {
        "Low": ("neutral", -0.1),
        "Medium": ("negative", -0.35),
        "High": ("negative", -0.55),
        "Critical": ("negative", -0.75),
    }[severity]


def _job_key(customer: str) -> str:
    return f"{customer}|demo|Demo Project"


def _pm_cycle(pm_names: list[str]):
    i = 0
    while True:
        yield pm_names[i % len(pm_names)]
        i += 1


def _estimator_cycle(estimator_names: list[str]):
    i = 0
    while True:
        yield estimator_names[i % len(estimator_names)]
        i += 1


def _real_pm_names() -> list[str]:
    from db.state_store import get_pm_list

    names = sorted({pm["full_name"] for pm in get_pm_list() if pm.get("full_name")})
    return names or ["Demo PM"]


def _real_estimator_names() -> list[str]:
    from db.state_store import _Session, _db_available, CaseyActiveJob

    if not _db_available:
        return FALLBACK_ESTIMATORS
    try:
        with _Session() as s:
            rows = s.query(CaseyActiveJob.estimator_name).filter(CaseyActiveJob.estimator_name.isnot(None)).all()
            names = sorted({r[0].strip() for r in rows if r[0] and r[0].strip()})
            return names or FALLBACK_ESTIMATORS
    except Exception:
        return FALLBACK_ESTIMATORS


def delete_demo_rows() -> dict:
    from db.state_store import (
        _Session, _db_available, CaseyFlagEvent, CaseyResponseWatch, MessageAnalysis, CaseyFlag, CustomerMessage,
    )

    if not _db_available:
        return {}
    counts = {}
    with _Session() as s:
        counts["flag_events"] = s.query(CaseyFlagEvent).filter(
            CaseyFlagEvent.flag_id.in_(s.query(CaseyFlag.id).filter(CaseyFlag.is_demo == True))  # noqa: E712
        ).delete(synchronize_session=False)
        counts["response_watches"] = s.query(CaseyResponseWatch).filter(CaseyResponseWatch.is_demo == True).delete(synchronize_session=False)  # noqa: E712
        counts["analyses"] = s.query(MessageAnalysis).filter(MessageAnalysis.is_demo == True).delete(synchronize_session=False)  # noqa: E712
        counts["flags"] = s.query(CaseyFlag).filter(CaseyFlag.is_demo == True).delete(synchronize_session=False)  # noqa: E712
        counts["messages"] = s.query(CustomerMessage).filter(CustomerMessage.is_demo == True).delete(synchronize_session=False)  # noqa: E712
        s.commit()
    return counts


def _insert_message(s, *, customer, pm_name, estimator_name, body_text, occurred_at,
                     external_id, job_key=None) -> int:
    from db.state_store import CustomerMessage

    message = CustomerMessage(
        source="demo", external_id=external_id, job_key=job_key or _job_key(customer),
        customer_email=f"{customer.lower().replace(' ', '.')}@example-demo.com",
        direction="inbound", occurred_at=occurred_at, sender=customer, body_text=body_text,
        is_automated=False, pm_name=pm_name, estimator_name=estimator_name, is_demo=True,
    )
    s.add(message)
    s.flush()
    return message.id


def _insert_analysis(s, *, message_id, sentiment, sentiment_score, severity, primary_reason,
                      is_strong_praise=False, red_flag_suggested=None, summary="", created_at=None) -> int:
    from db.state_store import MessageAnalysis

    if red_flag_suggested is None:
        red_flag_suggested = severity is not None
    analysis = MessageAnalysis(
        message_id=message_id, model="demo-seed", prompt_version="demo-seed-v1",
        sentiment=sentiment, sentiment_score=sentiment_score, is_strong_praise=is_strong_praise,
        red_flag_suggested=red_flag_suggested, suggested_severity=severity,
        suggested_primary_reason=primary_reason, suggested_reasons=[], suggested_root_cause="Undetermined (default)",
        summary=summary, status="ok", is_demo=True, created_at=created_at or datetime.now(timezone.utc),
    )
    s.add(analysis)
    s.flush()
    return analysis.id


def _insert_flag(s, *, job_key, customer, pm_name, estimator_name, severity, status,
                  primary_reason, root_cause, created_at, updated_at, closed_at=None,
                  resolution_reason=None, first_message_id=None, latest_message_id=None,
                  issue_summary="", dedup_key=None) -> int:
    from db.state_store import CaseyFlag

    flag = CaseyFlag(
        job_key=job_key, customer_name=customer, address=f"{customer}'s project (demo)",
        pm_name=pm_name, estimator_name=estimator_name, severity=severity, status=status,
        primary_reason=primary_reason, additional_reasons=[], root_cause=root_cause,
        issue_summary=issue_summary or f"{primary_reason} — demo flag for {customer}.",
        dedup_key=dedup_key or f"{job_key}:{primary_reason}:{created_at.isoformat()}",
        first_message_id=first_message_id, latest_message_id=latest_message_id or first_message_id,
        created_at=created_at, updated_at=updated_at, closed_at=closed_at, resolution_reason=resolution_reason,
        resolved_by="Chris S. (demo)" if closed_at else None, is_demo=True,
    )
    s.add(flag)
    s.flush()
    return flag.id


def _insert_event(s, *, flag_id, event_type, old_value, new_value, created_at, message_id=None) -> None:
    from db.state_store import CaseyFlagEvent

    s.add(CaseyFlagEvent(
        flag_id=flag_id, event_type=event_type, old_value=old_value, new_value=new_value,
        actor="casey", message_id=message_id, created_at=created_at, is_demo=True,
    ))


def seed_calibration_customers(s, now, pm_cycle, estimator_cycle) -> dict:
    quotes_by_customer = _load_calibration_quotes()
    stats = {"messages": 0, "flags": 0, "events": 0}

    # Julie Harrison: created High (9/26), later a Medium message that
    # never escalates further (Medium doesn't outrank the existing High)
    # — a natural "no_op" example.
    julie_pm, julie_est = next(pm_cycle), next(estimator_cycle)
    julie_job_key = _job_key("Julie Harrison")
    first_id = None
    flag_id = None
    for case in quotes_by_customer.get("Julie Harrison", []):
        occurred_at = datetime.strptime(case["date"], "%Y-%m-%d").replace(hour=14, tzinfo=timezone.utc)
        sentiment, score = _severity_to_sentiment(case["expected_severity"]) if case["expected_severity"] != "Low" else ("neutral", -0.1)
        message_id = _insert_message(
            s, customer="Julie Harrison", pm_name=julie_pm, estimator_name=julie_est,
            body_text=case["text"], occurred_at=occurred_at,
            external_id=f"demo-julie-{case['date']}", job_key=julie_job_key,
        )
        stats["messages"] += 1
        severity = case["expected_severity"] if case["expected_severity"] != "Low" else None
        _insert_analysis(
            s, message_id=message_id, sentiment=sentiment, sentiment_score=score, severity=severity,
            primary_reason="Lack of communication / no updates" if severity else None,
            summary=case["text"][:160], created_at=occurred_at,
        )
        if first_id is None:
            first_id = message_id
        if severity == "High" and flag_id is None:
            flag_id = _insert_flag(
                s, job_key=julie_job_key, customer="Julie Harrison", pm_name=julie_pm, estimator_name=julie_est,
                severity="High", status="Open", primary_reason="Lack of communication / no updates",
                root_cause="Undetermined (default)", created_at=occurred_at, updated_at=occurred_at,
                first_message_id=message_id, issue_summary="Customer feels taken advantage of; cause not yet confirmed.",
            )
            _insert_event(s, flag_id=flag_id, event_type="created", old_value=None, new_value="High",
                          created_at=occurred_at, message_id=message_id)
            _insert_event(s, flag_id=flag_id, event_type="notification_planned", old_value=None,
                          new_value="slack+email", created_at=occurred_at, message_id=message_id)
            stats["flags"] += 1
            stats["events"] += 2
        elif severity == "Medium" and flag_id is not None:
            _insert_event(s, flag_id=flag_id, event_type="note", old_value=None, new_value="no_op (Medium does not outrank High)",
                          created_at=occurred_at, message_id=message_id)
            stats["events"] += 1

    # Danielle Ewen: created High (10/1), escalated to Critical (10/19) —
    # the Medium/Low-boundary message on 10/5 stays unflagged history.
    danielle_pm, danielle_est = next(pm_cycle), next(estimator_cycle)
    danielle_job_key = _job_key("Danielle Ewen")
    flag_id = None
    for case in quotes_by_customer.get("Danielle Ewen", []):
        occurred_at = datetime.strptime(case["date"], "%Y-%m-%d").replace(hour=10, tzinfo=timezone.utc)
        severity = case["expected_severity"] if case["expected_severity"] != "Low" else None
        sentiment, score = _severity_to_sentiment(case["expected_severity"])
        message_id = _insert_message(
            s, customer="Danielle Ewen", pm_name=danielle_pm, estimator_name=danielle_est,
            body_text=case["text"], occurred_at=occurred_at,
            external_id=f"demo-danielle-{case['date']}", job_key=danielle_job_key,
        )
        stats["messages"] += 1
        _insert_analysis(
            s, message_id=message_id, sentiment=sentiment, sentiment_score=score, severity=severity,
            primary_reason="Estimate vs. final invoice gap" if severity else None,
            summary=case["text"][:160], created_at=occurred_at,
        )
        if severity == "High" and flag_id is None:
            flag_id = _insert_flag(
                s, job_key=danielle_job_key, customer="Danielle Ewen", pm_name=danielle_pm, estimator_name=danielle_est,
                severity="High", status="Open", primary_reason="Estimate vs. final invoice gap",
                root_cause="Undetermined (default)", created_at=occurred_at, updated_at=occurred_at,
                first_message_id=message_id,
                issue_summary="Customer has paid over $35,000 and feels they are receiving no services for it.",
            )
            _insert_event(s, flag_id=flag_id, event_type="created", old_value=None, new_value="High",
                          created_at=occurred_at, message_id=message_id)
            stats["flags"] += 1
            stats["events"] += 1
        elif severity == "Critical" and flag_id is not None:
            from db.state_store import CaseyFlag
            row = s.query(CaseyFlag).filter_by(id=flag_id).first()
            row.severity, row.updated_at, row.latest_message_id = "Critical", occurred_at, message_id
            row.issue_summary = "Customer describes the relationship as a \"failed partnership\" after prior billing concerns."
            _insert_event(s, flag_id=flag_id, event_type="severity_escalated", old_value="High", new_value="Critical",
                          created_at=occurred_at, message_id=message_id)
            _insert_event(s, flag_id=flag_id, event_type="notification_planned", old_value=None,
                          new_value="slack+email (escalation)", created_at=occurred_at, message_id=message_id)
            stats["events"] += 2

    # Caroline Day: single Medium (borderline High) flag, Open.
    caroline_pm, caroline_est = next(pm_cycle), next(estimator_cycle)
    caroline_job_key = _job_key("Caroline Day")
    for case in quotes_by_customer.get("Caroline Day", []):
        occurred_at = datetime.strptime(case["date"], "%Y-%m-%d").replace(hour=11, tzinfo=timezone.utc)
        message_id = _insert_message(
            s, customer="Caroline Day", pm_name=caroline_pm, estimator_name=caroline_est,
            body_text=case["text"], occurred_at=occurred_at,
            external_id=f"demo-caroline-{case['date']}", job_key=caroline_job_key,
        )
        stats["messages"] += 1
        _insert_analysis(
            s, message_id=message_id, sentiment="negative", sentiment_score=-0.5, severity="Medium",
            primary_reason="Punch list not completed", summary=case["text"][:160], created_at=occurred_at,
        )
        flag_id = _insert_flag(
            s, job_key=caroline_job_key, customer="Caroline Day", pm_name=caroline_pm, estimator_name=caroline_est,
            severity="Medium", status="Open", primary_reason="Punch list not completed",
            root_cause="Scheduling / capacity constraint", created_at=occurred_at, updated_at=occurred_at,
            first_message_id=message_id, issue_summary="Repeated schedule/material issues; trust is eroding.",
        )
        _insert_event(s, flag_id=flag_id, event_type="created", old_value=None, new_value="Medium",
                      created_at=occurred_at, message_id=message_id)
        stats["flags"] += 1
        stats["events"] += 1

    # Betsy Sandstrom: single Critical flag, closed as No Action Taken
    # (cancellation + refund already issued before this seed existed).
    betsy_pm, betsy_est = next(pm_cycle), next(estimator_cycle)
    betsy_job_key = _job_key("Betsy Sandstrom")
    for case in quotes_by_customer.get("Betsy Sandstrom", []):
        occurred_at = datetime.strptime(case["date"], "%Y-%m-%d").replace(hour=9, tzinfo=timezone.utc)
        message_id = _insert_message(
            s, customer="Betsy Sandstrom", pm_name=betsy_pm, estimator_name=betsy_est,
            body_text=case["text"], occurred_at=occurred_at,
            external_id=f"demo-betsy-{case['date']}", job_key=betsy_job_key,
        )
        stats["messages"] += 1
        _insert_analysis(
            s, message_id=message_id, sentiment="negative", sentiment_score=-0.85, severity="Critical",
            primary_reason="Schedule delay / missed dates", summary=case["text"][:160], created_at=occurred_at,
        )
        closed_at = occurred_at + timedelta(days=2)
        flag_id = _insert_flag(
            s, job_key=betsy_job_key, customer="Betsy Sandstrom", pm_name=betsy_pm, estimator_name=betsy_est,
            severity="Critical", status="No Action Taken", primary_reason="Schedule delay / missed dates",
            root_cause="Scheduling / capacity constraint", created_at=occurred_at, updated_at=closed_at,
            closed_at=closed_at, resolution_reason="Customer cancelled contract; refund issued.",
            first_message_id=message_id, issue_summary="Customer cancelled after repeated delays; refund issued.",
        )
        _insert_event(s, flag_id=flag_id, event_type="created", old_value=None, new_value="Critical",
                      created_at=occurred_at, message_id=message_id)
        _insert_event(s, flag_id=flag_id, event_type="status_changed", old_value="Open", new_value="No Action Taken",
                      created_at=closed_at, message_id=message_id)
        stats["flags"] += 1
        stats["events"] += 2

    return stats


def seed_invented_flags(s, now, pm_cycle, estimator_cycle) -> dict:
    stats = {"messages": 0, "flags": 0, "events": 0}
    quote_counts: dict[str, int] = {}

    for customer, severity, status, reason, root_cause, days_ago, resolution, escalation_from, improving in INVENTED_FLAGS:
        pm_name = next(pm_cycle)
        estimator_name = next(estimator_cycle)
        created_at = now - timedelta(days=days_ago, hours=3)
        job_key = _job_key(customer)

        quotes = INVENTED_MESSAGES.get(customer, [])
        quote_index = quote_counts.get(customer, 0)
        quote_counts[customer] = quote_index + 1
        if quotes:
            text, sentiment, score = quotes[quote_index % len(quotes)]
        else:
            text, sentiment, score = f"Ongoing concern about {reason.lower()}.", "negative", -0.4

        message_id = _insert_message(
            s, customer=customer, pm_name=pm_name, estimator_name=estimator_name, body_text=text,
            occurred_at=created_at, external_id=f"demo-{customer.lower().replace(' ', '-')}-{days_ago}-{reason[:10]}",
            job_key=job_key,
        )
        stats["messages"] += 1
        _insert_analysis(
            s, message_id=message_id, sentiment=sentiment, sentiment_score=score, severity=severity,
            primary_reason=reason, summary=text[:160], created_at=created_at,
        )

        closed_at = None
        resolution_reason = None
        updated_at = created_at
        if resolution:
            resolution_reason, days_to_close_ago = resolution
            closed_at = now - timedelta(days=days_to_close_ago)
            updated_at = closed_at

        if escalation_from:
            escalation_at = created_at + timedelta(days=2)
            flag_created_at = created_at
        else:
            flag_created_at = created_at

        flag_id = _insert_flag(
            s, job_key=job_key, customer=customer, pm_name=pm_name, estimator_name=estimator_name,
            severity=escalation_from or severity, status=status, primary_reason=reason, root_cause=root_cause,
            created_at=flag_created_at, updated_at=updated_at, closed_at=closed_at,
            resolution_reason=resolution_reason, first_message_id=message_id, issue_summary=text[:200],
            dedup_key=f"{job_key}:{reason}",
        )
        stats["flags"] += 1
        _insert_event(s, flag_id=flag_id, event_type="created", old_value=None,
                      new_value=escalation_from or severity, created_at=flag_created_at, message_id=message_id)
        stats["events"] += 1

        if escalation_from:
            from db.state_store import CaseyFlag
            escalation_at = flag_created_at + timedelta(days=2)
            row = s.query(CaseyFlag).filter_by(id=flag_id).first()
            row.severity = severity
            row.updated_at = escalation_at
            _insert_event(s, flag_id=flag_id, event_type="severity_escalated", old_value=escalation_from,
                          new_value=severity, created_at=escalation_at, message_id=message_id)
            stats["events"] += 1

        if improving:
            improving_at = flag_created_at + timedelta(days=1)
            _insert_event(s, flag_id=flag_id, event_type="improving_sentiment", old_value=None, new_value="positive",
                          created_at=improving_at, message_id=message_id)
            stats["events"] += 1

        if status in ("Resolved", "No Action Taken"):
            _insert_event(s, flag_id=flag_id, event_type="status_changed", old_value="Open", new_value=status,
                          created_at=closed_at or updated_at, message_id=message_id)
            stats["events"] += 1

    return stats


def seed_strong_praise(s, now, pm_cycle, estimator_cycle) -> dict:
    stats = {"messages": 0}
    for i, (customer, text) in enumerate(STRONG_PRAISE):
        pm_name = next(pm_cycle)
        estimator_name = next(estimator_cycle)
        occurred_at = now - timedelta(days=(i + 1) * 5, hours=2)
        message_id = _insert_message(
            s, customer=customer, pm_name=pm_name, estimator_name=estimator_name, body_text=text,
            occurred_at=occurred_at, external_id=f"demo-praise-{i}",
        )
        _insert_analysis(
            s, message_id=message_id, sentiment="positive", sentiment_score=0.9, severity=None,
            primary_reason=None, is_strong_praise=True, red_flag_suggested=False,
            summary=text[:160], created_at=occurred_at,
        )
        stats["messages"] += 1
    return stats


def seed_response_watches(s, now, pm_cycle) -> dict:
    stats = {"watches": 0, "messages": 0}
    for customer, status, received_hours_ago, due_offset_hours, responded_hours_after in RESPONSE_WATCHES:
        pm_name = next(pm_cycle)
        received_at = now - timedelta(hours=received_hours_ago)
        due_at = received_at + timedelta(hours=due_offset_hours)
        message_id = _insert_message(
            s, customer=customer, pm_name=pm_name, estimator_name=None,
            body_text="Just checking in on the project timeline.", occurred_at=received_at,
            external_id=f"demo-watch-{customer.lower().replace(' ', '-')}",
        )
        responded_at = None
        if responded_hours_after is not None:
            responded_at = received_at + timedelta(hours=responded_hours_after)

        from db.state_store import CaseyResponseWatch
        s.add(CaseyResponseWatch(
            thread_id=f"demo-thread-{customer}", inbound_message_id=message_id, job_key=_job_key(customer),
            pm_name=pm_name, customer_email=f"{customer.lower().replace(' ', '.')}@example-demo.com",
            received_at=received_at, due_at=due_at, status=status, responded_at=responded_at, is_demo=True,
        ))
        stats["watches"] += 1
        stats["messages"] += 1
    return stats


def run(reset: bool = False) -> dict:
    from db.state_store import _Session, _db_available, CaseyFlag

    if not _db_available:
        return {"ok": False, "error": "database not available"}

    with _Session() as s:
        existing_flags = s.query(CaseyFlag).filter(CaseyFlag.is_demo == True).count()  # noqa: E712

    if reset:
        deleted = delete_demo_rows()
        logger.info(f"seed_casey_demo --reset: deleted {deleted}")
    elif existing_flags > 0:
        return {"ok": True, "skipped": True, "existing_flags": existing_flags}

    pm_names = _real_pm_names()
    estimator_names = _real_estimator_names()
    now = datetime.now(timezone.utc)

    stats = {"messages": 0, "flags": 0, "events": 0, "watches": 0}
    with _Session() as s:
        pm_cycle, estimator_cycle = _pm_cycle(pm_names), _estimator_cycle(estimator_names)
        calib_stats = seed_calibration_customers(s, now, pm_cycle, estimator_cycle)
        invented_stats = seed_invented_flags(s, now, pm_cycle, estimator_cycle)
        praise_stats = seed_strong_praise(s, now, pm_cycle, estimator_cycle)
        watch_stats = seed_response_watches(s, now, pm_cycle)
        s.commit()

    for d in (calib_stats, invented_stats, praise_stats, watch_stats):
        for k, v in d.items():
            stats[k] = stats.get(k, 0) + v

    return {"ok": True, "skipped": False, **stats, "pm_names_used": pm_names, "estimator_names_used": estimator_names}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="Delete existing demo rows first, then reseed.")
    args = parser.parse_args()

    result = run(reset=args.reset)
    if not result.get("ok"):
        print(f"ERROR: {result.get('error')}", file=sys.stderr)
        sys.exit(1)
    if result.get("skipped"):
        print(f"Demo data already present ({result['existing_flags']} flags) — pass --reset to reseed.")
        return
    print(f"Seeded: {result['messages']} messages, {result['flags']} flags, "
          f"{result['events']} events, {result['watches']} response watches.")
    print(f"PMs used: {', '.join(result['pm_names_used'])}")
    print(f"Estimators used: {', '.join(result['estimator_names_used'])}")


if __name__ == "__main__":
    main()
