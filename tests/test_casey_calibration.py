import pytest

from agents.casey.calibration import load_calibration_set
from agents.casey.taxonomy import SEVERITIES


def test_loads_default_fixture():
    cases = load_calibration_set()
    assert len(cases) == 9


def test_every_case_has_required_keys():
    cases = load_calibration_set()
    required = {"customer", "date", "text", "expected_severity", "boundary_note"}
    for case in cases:
        assert required <= case.keys()


def test_every_expected_severity_is_valid():
    cases = load_calibration_set()
    for case in cases:
        assert case["expected_severity"] in SEVERITIES


def test_all_four_severities_represented():
    cases = load_calibration_set()
    seen = {case["expected_severity"] for case in cases}
    assert seen == set(SEVERITIES)


def test_sandstrom_history_is_flagged_as_a_summary_not_a_quote():
    cases = load_calibration_set()
    sandstrom_summary = next(
        c for c in cases if c["customer"] == "Betsy Sandstrom" and c["date"] is None
    )
    assert "summary" in sandstrom_summary["boundary_note"].lower()
    assert "not a verbatim quote" in sandstrom_summary["boundary_note"].lower()


def test_rejects_case_missing_required_key(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text('[{"customer": "X", "date": "2026-01-01", "text": "hi"}]')
    with pytest.raises(ValueError, match="missing required keys"):
        load_calibration_set(bad_file)


def test_rejects_invalid_severity(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(
        '[{"customer": "X", "date": "2026-01-01", "text": "hi", '
        '"expected_severity": "Severe", "boundary_note": null}]'
    )
    with pytest.raises(ValueError, match="is not in"):
        load_calibration_set(bad_file)


def test_rejects_empty_list(tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("[]")
    with pytest.raises(ValueError, match="non-empty"):
        load_calibration_set(bad_file)
