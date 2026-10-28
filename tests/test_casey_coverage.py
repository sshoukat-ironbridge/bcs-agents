import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from casey_coverage import compute_coverage  # noqa: E402

PM_LIST = [
    {"full_name": "Gustavo", "email": "gustavo@bcs.com", "dialpad_user_id": "123"},
    {"full_name": "Andrea", "email": "andrea@bcs.com", "dialpad_user_id": ""},
    {"full_name": "NoEmail", "email": "", "dialpad_user_id": "999"},
]


def _job(client_name, pm_name="", comms_hold=False, retired=False):
    return {"client_name": client_name, "pm_name": pm_name, "comms_hold": comms_hold, "retired": retired}


def _find_pm(name, pm_list=None):
    pm_list = pm_list or PM_LIST
    for pm in pm_list:
        if pm["full_name"].lower() == (name or "").strip().lower():
            return pm
    return None


def test_monitored_counts_by_pm():
    jobs = [_job("A", "Gustavo"), _job("B", "Gustavo"), _job("C", "Andrea")]
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    assert result["monitored_by_pm"] == {"Gustavo": 2, "Andrea": 1}
    assert result["monitored_total"] == 3


def test_on_hold_jobs_are_not_monitored():
    jobs = [_job("A", "Gustavo", comms_hold=True)]
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    assert result["monitored_total"] == 0
    assert result["not_monitored"]["on_hold"] == 1


def test_blank_pm_is_not_monitored():
    jobs = [_job("A", "")]
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    assert result["not_monitored"]["blank_pm"] == 1


def test_unrecognized_pm_name_is_grouped_with_examples():
    jobs = [_job("A", "Bogus PM"), _job("B", "Bogus PM"), _job("C", "Bogus PM"), _job("D", "Bogus PM")]
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    entry = result["not_monitored"]["unrecognized_pm_name"]["Bogus PM"]
    assert entry["count"] == 4
    assert len(entry["examples"]) == 3  # capped at 3 examples


def test_pm_missing_email_is_not_monitored():
    jobs = [_job("A", "NoEmail")]
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    assert result["not_monitored"]["missing_email"] == {"NoEmail": 1}
    assert result["monitored_total"] == 0


def test_monitored_but_missing_dialpad_id_is_flagged_not_excluded():
    jobs = [_job("A", "Andrea")]  # Andrea has email but no dialpad_user_id
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    assert result["monitored_total"] == 1  # still monitored via email
    assert result["monitored_missing_dialpad"] == {"Andrea": 1}


def test_retired_jobs_are_excluded_entirely():
    jobs = [_job("A", "Gustavo", retired=True), _job("B", "Gustavo", retired=False)]
    with patch("db.state_store.get_all_active_jobs", return_value=jobs), \
         patch("db.state_store.get_pm_list", return_value=PM_LIST), \
         patch("db.state_store.find_pm", side_effect=_find_pm):
        result = compute_coverage()
    assert result["total_active_jobs"] == 1
    assert result["monitored_total"] == 1
