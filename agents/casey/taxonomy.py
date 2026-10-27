"""Casey's complaint/severity taxonomy, verbatim from Chris's v1.3 doc.

Pure constants and small rule functions only; no DB/network access.
"""

# --- Severities (ordered, with rank: higher rank = more severe) ---

SEVERITIES = ["Low", "Medium", "High", "Critical"]
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def severity_rank(severity: str) -> int:
    """Rank for comparison; raises KeyError for an unknown severity (callers
    should validate against SEVERITIES first)."""
    return SEVERITY_RANK[severity]


SEVERITY_GUIDE = {
    "Low": (
        "Early isolated concern or confusion, customer cooperative."
    ),
    "Medium": (
        "Clear frustration, recurring delay or communication/quality "
        "problem, relationship viable."
    ),
    "High": (
        "Significant dissatisfaction, repeated failed commitments, "
        "declining confidence."
    ),
    "Critical": (
        "Strong indicators of imminent breakdown even without a formal "
        "threat (questions whether BCS can continue, \"failed partnership\", "
        "profound loss of trust, one more failure may end it), plus "
        "refund/cancellation demands, legal threats, serious safety hazards."
    ),
}


# --- Statuses ---

STATUSES = ["Open", "In Process", "Resolved", "No Action Taken"]


# --- Reasons (primary plus additional allowed) ---

REASONS = [
    "Workmanship / quality of work",
    "Rework required",
    "Punch list not completed",
    "Materials defect / wrong materials",
    "Schedule delay / missed dates",
    "Job not started on time",
    "Lack of communication / no updates",
    "Unresponsive PM or estimator",
    "Estimate vs. final invoice gap",
    "Unexpected charges",
    "Pricing or billing dispute",
    "Scope / change order confusion",
    "Cleanliness / site condition",
    "Crew professionalism / conduct",
    "Property or landscape damage",
    "Warranty or rework response time",
    "Permitting / inspection issue",
    "Safety concern",
    "Other (explain in Notes)",
]

REASON_OTHER = "Other (explain in Notes)"


# --- Root causes ---

ROOT_CAUSES = [
    "Estimating error / scope gap",
    "Scheduling / capacity constraint",
    "Subcontractor performance",
    "In-house crew performance",
    "Supplier / material delay",
    "Internal communication breakdown",
    "Process gap / no standard exists",
    "Standard exists / not followed",
    "Training or skill gap",
    "Documentation / paperwork error",
    "Customer-driven change",
    "Outside our control (weather, jurisdiction)",
    "Undetermined (default)",
]

ROOT_CAUSE_DEFAULT = "Undetermined (default)"


def default_root_cause() -> str:
    return ROOT_CAUSE_DEFAULT


def validate_reasons(primary_reason: str, additional_reasons: "list[str] | None" = None,
                      notes: str = "") -> "list[str]":
    """Returns a list of validation error strings (empty list = valid).

    Rule: "Other (explain in Notes)" (as the primary reason, or among the
    additional reasons) requires non-empty notes.
    """
    errors = []
    if primary_reason not in REASONS:
        errors.append(f"primary_reason {primary_reason!r} is not a recognized reason")
    additional_reasons = additional_reasons or []
    for reason in additional_reasons:
        if reason not in REASONS:
            errors.append(f"additional reason {reason!r} is not a recognized reason")

    uses_other = primary_reason == REASON_OTHER or REASON_OTHER in additional_reasons
    if uses_other and not notes.strip():
        errors.append(f"{REASON_OTHER!r} requires explanatory notes")
    return errors
