from agents.casey.taxonomy import (
    SEVERITIES, SEVERITY_RANK, SEVERITY_GUIDE, STATUSES, REASONS, REASON_OTHER,
    ROOT_CAUSES, ROOT_CAUSE_DEFAULT, severity_rank, default_root_cause, validate_reasons,
)


def test_severities_ordered_low_to_critical():
    assert SEVERITIES == ["Low", "Medium", "High", "Critical"]
    assert severity_rank("Low") < severity_rank("Medium") < severity_rank("High") < severity_rank("Critical")


def test_severity_rank_matches_dict():
    for name, rank in SEVERITY_RANK.items():
        assert severity_rank(name) == rank


def test_severity_guide_has_all_four():
    assert set(SEVERITY_GUIDE.keys()) == set(SEVERITIES)
    for text in SEVERITY_GUIDE.values():
        assert text.strip()


def test_statuses_exact():
    assert STATUSES == ["Open", "In Process", "Resolved", "No Action Taken"]


def test_reasons_count_and_other_present():
    assert len(REASONS) == 19
    assert REASON_OTHER == "Other (explain in Notes)"
    assert REASON_OTHER in REASONS


def test_root_causes_default_is_undetermined():
    assert ROOT_CAUSE_DEFAULT == "Undetermined (default)"
    assert ROOT_CAUSE_DEFAULT in ROOT_CAUSES
    assert default_root_cause() == ROOT_CAUSE_DEFAULT


def test_other_reason_requires_notes():
    errors = validate_reasons(REASON_OTHER, notes="")
    assert any("requires explanatory notes" in e for e in errors)

    errors = validate_reasons(REASON_OTHER, notes="customer says the cabinet color is wrong")
    assert errors == []


def test_other_in_additional_reasons_also_requires_notes():
    errors = validate_reasons("Rework required", additional_reasons=[REASON_OTHER], notes="")
    assert any("requires explanatory notes" in e for e in errors)


def test_non_other_reason_does_not_require_notes():
    errors = validate_reasons("Rework required", notes="")
    assert errors == []


def test_unrecognized_reason_is_flagged():
    errors = validate_reasons("Made up reason", notes="")
    assert any("is not a recognized reason" in e for e in errors)
