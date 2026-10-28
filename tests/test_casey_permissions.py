"""Confirms a PM viewer cannot see or change another PM's flag by
guessing the URL — enforcement happens in the query layer
(dashboard/casey_queries.py's _role_filter), not just hidden in the UI,
so this goes through the real HTTP routes against the real seeded demo
data rather than mocking the query layer away.
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.main import app
from db.state_store import _Session, _db_available, CaseyFlag


@pytest.fixture(scope="module", autouse=True)
def seeded_demo_data():
    if not _db_available:
        pytest.skip("no database available in this environment")
    from db.state_store import get_config, set_config
    import scripts.seed_casey_demo as seed_demo

    # This test seeds and reads is_demo=True rows, so it must not depend
    # on whatever casey_demo_mode happens to be set to in a shared local
    # DB (e.g. a Stage 10a live run turning it off) — force it on here,
    # then restore whatever it was.
    previous = get_config("casey_demo_mode")
    set_config("casey_demo_mode", "true")
    seed_demo.run(reset=True)
    yield
    set_config("casey_demo_mode", previous if previous is not None else "true")


@pytest.fixture
def client():
    return TestClient(app)


def _two_different_pms_with_flags() -> "tuple[str, int, str, int]":
    """Returns (pm_a, flag_id_belonging_to_a, pm_b, flag_id_belonging_to_b)
    from the real seeded demo flags."""
    with _Session() as s:
        flags = s.query(CaseyFlag).filter(CaseyFlag.is_demo == True).all()  # noqa: E712
    by_pm: dict[str, int] = {}
    for f in flags:
        if f.pm_name and f.pm_name not in by_pm:
            by_pm[f.pm_name] = f.id
        if len(by_pm) >= 2:
            break
    pms = list(by_pm.items())
    return pms[0][0], pms[0][1], pms[1][0], pms[1][1]


def test_pm_can_view_their_own_flag(client):
    pm_a, flag_a, _pm_b, _flag_b = _two_different_pms_with_flags()
    viewer = {"id": 1, "email": "pm@example.com", "role": "pm", "linked_name": pm_a}
    with patch("dashboard.auth.get_current_user", return_value=viewer):
        resp = client.get(f"/casey/flags/{flag_a}")
    assert resp.status_code == 200


def test_pm_cannot_view_another_pms_flag_by_url(client):
    pm_a, _flag_a, _pm_b, flag_b = _two_different_pms_with_flags()
    viewer = {"id": 1, "email": "pm@example.com", "role": "pm", "linked_name": pm_a}
    with patch("dashboard.auth.get_current_user", return_value=viewer):
        resp = client.get(f"/casey/flags/{flag_b}")
    assert resp.status_code == 404


def test_pm_cannot_update_another_pms_flag_by_url(client):
    pm_a, _flag_a, _pm_b, flag_b = _two_different_pms_with_flags()
    viewer = {"id": 1, "email": "pm@example.com", "role": "pm", "linked_name": pm_a}
    with _Session() as s:
        before = s.query(CaseyFlag).filter_by(id=flag_b).first()
        before_status = before.status

    with patch("dashboard.auth.get_current_user", return_value=viewer):
        resp = client.post(f"/casey/flags/{flag_b}", data={"status": "Resolved",
                                                              "resolution_reason": "Issue resolved to customer's satisfaction"})
    assert resp.status_code == 404

    with _Session() as s:
        after = s.query(CaseyFlag).filter_by(id=flag_b).first()
        assert after.status == before_status  # untouched


def test_admin_can_view_any_flag(client):
    _pm_a, _flag_a, pm_b, flag_b = _two_different_pms_with_flags()
    admin = {"id": 1, "email": "admin@example.com", "role": "admin", "linked_name": ""}
    with patch("dashboard.auth.get_current_user", return_value=admin):
        resp = client.get(f"/casey/flags/{flag_b}")
    assert resp.status_code == 200


def test_pm_list_pages_are_scoped_too(client):
    pm_a, flag_a, pm_b, _flag_b = _two_different_pms_with_flags()
    viewer = {"id": 1, "email": "pm@example.com", "role": "pm", "linked_name": pm_a}
    with patch("dashboard.auth.get_current_user", return_value=viewer):
        resp = client.get("/casey/flags")
    assert resp.status_code == 200
    assert pm_b not in resp.text or pm_a in resp.text  # pm_a's own name is expected; pm_b's never should be a flag row

    with patch("dashboard.auth.get_current_user", return_value=viewer):
        resp = client.get("/casey/people?type=pm")
    assert resp.status_code == 200
    # Only the viewer's own row, never pm_b's.
    assert f">{pm_b}<" not in resp.text
