from unittest.mock import patch

from agents.casey.main import run_stages, _format_summary, ALL_STAGES


def _config(overrides=None):
    values = {"casey_dialpad_enabled": "false", "casey_gmail_enabled": "false", "casey_analysis_enabled": "false"}
    values.update(overrides or {})
    return lambda key: values.get(key, "")


def test_disabled_stages_are_skipped():
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("agents.casey.process_flags.run", return_value={"processed": 0}), \
         patch("agents.casey.run_watches.run", return_value={"created": 0}):
        results = run_stages(dry_run=True)
    assert results["ingest_dialpad"] == {"ok": True, "skipped": True}
    assert results["ingest_gmail"] == {"ok": True, "skipped": True}
    assert results["analyze"] == {"ok": True, "skipped": True}
    assert results["process_flags"]["ok"] is True
    assert results["run_watches"]["ok"] is True


def test_enabled_stages_actually_run():
    with patch("db.state_store.get_config", side_effect=_config({
            "casey_dialpad_enabled": "true", "casey_gmail_enabled": "true", "casey_analysis_enabled": "true"})), \
         patch("agents.casey.ingest_dialpad.run", return_value={"calls_seen": 5}) as mock_dialpad, \
         patch("agents.casey.ingest_gmail.run", return_value={"stored": 1}) as mock_gmail, \
         patch("agents.casey.analyze.run", return_value={"analyzed": 2}) as mock_analyze, \
         patch("agents.casey.process_flags.run", return_value={"processed": 0}), \
         patch("agents.casey.run_watches.run", return_value={"created": 0}):
        results = run_stages(dry_run=False)
    mock_dialpad.assert_called_once()
    mock_gmail.assert_called_once()
    mock_analyze.assert_called_once()
    assert results["ingest_dialpad"]["stats"] == {"calls_seen": 5}
    assert results["ingest_gmail"]["stats"] == {"stored": 1}
    assert results["analyze"]["stats"] == {"analyzed": 2}


def test_one_stage_failure_does_not_block_the_rest():
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("agents.casey.process_flags.run", side_effect=RuntimeError("boom")), \
         patch("agents.casey.run_watches.run", return_value={"created": 3}) as mock_watches:
        results = run_stages(dry_run=True)
    assert results["process_flags"]["ok"] is False
    assert "boom" in results["process_flags"]["error"]
    mock_watches.assert_called_once()
    assert results["run_watches"]["ok"] is True


def test_stages_filter_runs_only_the_named_subset():
    with patch("db.state_store.get_config", side_effect=_config()), \
         patch("agents.casey.process_flags.run", return_value={"processed": 0}) as mock_process, \
         patch("agents.casey.run_watches.run") as mock_watches:
        results = run_stages(dry_run=True, stages=["process_flags"])
    assert list(results.keys()) == ["process_flags"]
    mock_process.assert_called_once()
    mock_watches.assert_not_called()


def test_unknown_stage_name_is_skipped_with_a_warning():
    with patch("db.state_store.get_config", side_effect=_config()):
        results = run_stages(dry_run=True, stages=["not_a_real_stage"])
    assert results == {}


def test_all_stages_constant_matches_spec_order():
    assert ALL_STAGES == ["ingest_dialpad", "ingest_gmail", "analyze", "process_flags", "run_watches"]


def test_format_summary_includes_every_stage():
    results = {
        "ingest_dialpad": {"ok": True, "skipped": True},
        "process_flags": {"ok": True, "stats": {"processed": 3}},
        "run_watches": {"ok": False, "error": "db down"},
    }
    summary = _format_summary(results)
    assert "ingest_dialpad: skipped" in summary
    assert "process_flags: ok" in summary and "'processed': 3" in summary
    assert "run_watches: FAILED" in summary and "db down" in summary
