from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from dashboard.main import app

SECRET = "test-webhook-secret"
PATH = "/webhooks/dialpad/sms"


@pytest.fixture
def client():
    return TestClient(app)


def _signed_payload(secret=SECRET, **overrides):
    payload = {"id": 123, "direction": "inbound", "text": "hello", "from_number": "+15551234567"}
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def test_valid_signature_is_accepted_and_stored(client):
    with patch("dashboard.routes.webhooks.cfg", return_value=SECRET), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=_signed_payload())
    assert resp.status_code == 200
    mock_store.assert_called_once()
    args = mock_store.call_args[0]
    assert args[0] == "dialpad_sms"
    assert args[1] is True  # signature_valid
    assert args[2]["text"] == "hello"


def test_invalid_signature_is_recorded_but_still_acked(client):
    wrong_secret_token = _signed_payload(secret="wrong-secret")
    with patch("dashboard.routes.webhooks.cfg", return_value=SECRET), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=wrong_secret_token)
    assert resp.status_code == 200
    args = mock_store.call_args[0]
    assert args[1] is False  # signature_valid


def test_missing_secret_rejects_by_default(client, monkeypatch):
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    with patch("dashboard.routes.webhooks.cfg", return_value=""), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=_signed_payload())
    assert resp.status_code == 401
    mock_store.assert_not_called()


def test_missing_secret_accepted_when_unsigned_allowed(client, monkeypatch):
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "1")
    with patch("dashboard.routes.webhooks.cfg", return_value=""), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content='{"id": 1, "text": "hi"}')
    assert resp.status_code == 200
    args = mock_store.call_args[0]
    assert args[1] is False  # signature_valid - nothing was verified


def test_oversized_body_is_rejected(client):
    huge_body = b"x" * (64 * 1024 + 1)
    with patch("dashboard.routes.webhooks.cfg", return_value=SECRET), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=huge_body, headers={"content-length": str(len(huge_body))})
    assert resp.status_code == 413
    mock_store.assert_not_called()


def test_other_dashboard_routes_still_require_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_webhook_path_bypasses_login():
    # No session cookie at all, and it's a 200/401 from the handler itself
    # (secret logic), never a login redirect.
    client = TestClient(app)
    with patch("dashboard.routes.webhooks.cfg", return_value=SECRET), \
         patch("dashboard.routes.webhooks._store_event"):
        resp = client.post(PATH, content=_signed_payload(), follow_redirects=False)
    assert resp.status_code != 302
