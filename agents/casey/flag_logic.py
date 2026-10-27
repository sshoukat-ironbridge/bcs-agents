"""Decides what to do with a new message analysis against a job's existing
open (non-closed) flags. Pure function, no DB/network access.

Same-complaint test (configurable): two flags/candidates are the "same
underlying complaint" when they share job_key AND primary_reason. Chris's
doc calls for deduping the same complaint while keeping unrelated
complaints on the same job as separate flags.
"""
from typing import Any

from agents.casey.taxonomy import severity_rank

ACTIONS = ["create", "escalate", "note_improvement", "no_op", "create_separate"]


def is_same_complaint(flag: dict, candidate: dict, same_complaint_by: str = "primary_reason") -> bool:
    return (
        flag.get("job_key") == candidate.get("job_key")
        and flag.get(same_complaint_by) == candidate.get(same_complaint_by)
    )


def find_matching_flag(existing_open_flags: list[dict], candidate: dict,
                        same_complaint_by: str = "primary_reason") -> "dict | None":
    for flag in existing_open_flags:
        if is_same_complaint(flag, candidate, same_complaint_by):
            return flag
    return None


def decide_action(existing_open_flags: list[dict], candidate: dict,
                   same_complaint_by: str = "primary_reason") -> dict[str, Any]:
    """existing_open_flags: this job's currently-open (non-closed) flags —
    the caller filters to one job_key before calling, since dedup/escalation
    only ever happens within the same job.

    candidate: {"job_key", "primary_reason", "severity", "red_flag_suggested"
    (bool), "sentiment" ("positive"|"neutral"|"negative", optional)}.

    Returns {"action": one of ACTIONS, "matched_flag": dict | None}.

    Rules:
      - Same complaint (job_key + primary_reason) already has an open flag:
          * candidate's severity ranks higher -> escalate (notify again
            with the new severity).
          * candidate is not a red flag, or sentiment is improving
            ("positive") -> note_improvement (recorded as an event; NEVER
            closes or lowers the flag automatically — only a human does).
          * otherwise -> no_op (same severity/complaint, nothing changes).
      - No open flag for this complaint, but a red flag is suggested:
          * another open flag already exists on this job (different
            complaint) -> create_separate (keep them as distinct flags).
          * otherwise -> create.
      - No red flag suggested and no matching open flag -> no_op.
    """
    matched = find_matching_flag(existing_open_flags, candidate, same_complaint_by)

    if matched is not None:
        if candidate.get("red_flag_suggested") and _ranks_higher(candidate.get("severity"), matched.get("severity")):
            return {"action": "escalate", "matched_flag": matched}
        if not candidate.get("red_flag_suggested") or candidate.get("sentiment") == "positive":
            return {"action": "note_improvement", "matched_flag": matched}
        return {"action": "no_op", "matched_flag": matched}

    if candidate.get("red_flag_suggested"):
        if existing_open_flags:
            return {"action": "create_separate", "matched_flag": None}
        return {"action": "create", "matched_flag": None}

    return {"action": "no_op", "matched_flag": None}


def _ranks_higher(candidate_severity: "str | None", existing_severity: "str | None") -> bool:
    if candidate_severity is None or existing_severity is None:
        return False
    try:
        return severity_rank(candidate_severity) > severity_rank(existing_severity)
    except KeyError:
        return False
