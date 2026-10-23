from unittest.mock import patch

import pytest

from agents.oca.main import _run
from integrations.sheets import SheetFetchError


def test_sheet_fetch_failure_aborts_before_upsert_or_retire():
    """If get_active_jobs() raises SheetFetchError, _run() must propagate
    it immediately — before calling _sync_all() (which upserts) or
    reconcile_retired_jobs() (which retires). No DB connection is made;
    everything that would touch it is mocked."""
    with patch("agents.oca.main.cfg", return_value="configured"), \
         patch("agents.oca.main.init_db"), \
         patch("agents.oca.main.get_active_jobs", side_effect=SheetFetchError("boom")), \
         patch("agents.oca.main._sync_all") as mock_sync_all, \
         patch("agents.oca.main.reconcile_retired_jobs") as mock_reconcile:

        with pytest.raises(SheetFetchError):
            _run(run_id=None)

        mock_sync_all.assert_not_called()
        mock_reconcile.assert_not_called()


def test_normal_sheet_fetch_does_not_raise_at_the_guard():
    """Sanity check that the guards themselves don't block a configured run
    (so the abort test above is actually exercising the sheet-fetch path,
    not just failing on missing config)."""
    with patch("agents.oca.main.cfg", return_value="configured"), \
         patch("agents.oca.main.init_db"), \
         patch("agents.oca.main.get_active_jobs", return_value=[]):
        # Empty sheet_jobs (not an error) aborts cleanly with a message,
        # it doesn't raise.
        result = _run(run_id=None)
        assert "no sheet jobs loaded" in result.lower()
