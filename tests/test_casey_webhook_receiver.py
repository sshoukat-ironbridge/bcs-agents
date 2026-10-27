from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from dashboard.main import app
from dashboard.routes.webhooks import SlidingWindowRateLimiter

SECRET = "test-webhook-secret"
PATH = "/webhooks/dialpad/sms"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    # Each test gets a clean rate-limit/counter state, independent of
    # whatever ran before it.
    import dashboard.routes.webhooks as webhooks_mod
    webhooks_mod._rate_limiter.reset()
    webhooks_mod._invalid_signature_counts.clear()
    yield


def _signed_payload(secret=SECRET, **overrides):
    payload = {"id": 123, "direction": "inbound", "text": "hello", "from_number": "+15551234567"}
    payload.update(overrides)
    return jwt.encode(payload, secret, algorithm="HS256")


def _cfg_with_secret(secret=SECRET, rate_limit="30"):
    values = {"dialpad_webhook_secret": secret, "casey_webhook_rate_limit_per_minute": rate_limit}
    return lambda key: values.get(key, "")


def test_valid_signature_is_stored(client):
    with patch("dashboard.routes.webhooks.cfg", side_effect=_cfg_with_secret()), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=_signed_payload())
    assert resp.status_code == 200
    mock_store.assert_called_once()
    args = mock_store.call_args[0]
    assert args[0] == "dialpad_sms"
    assert args[1]["text"] == "hello"


def test_invalid_signature_is_never_stored(client):
    wrong_secret_token = _signed_payload(secret="wrong-secret")
    with patch("dashboard.routes.webhooks.cfg", side_effect=_cfg_with_secret()), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=wrong_secret_token)
    assert resp.status_code == 200
    mock_store.assert_not_called()


def test_invalid_signature_increments_counter(client):
    import dashboard.routes.webhooks as webhooks_mod
    wrong_secret_token = _signed_payload(secret="wrong-secret")
    with patch("dashboard.routes.webhooks.cfg", side_effect=_cfg_with_secret()):
        client.post(PATH, content=wrong_secret_token)
        client.post(PATH, content=wrong_secret_token)
    assert webhooks_mod._invalid_signature_counts["testclient"] == 2


def test_missing_secret_rejects_by_default(client, monkeypatch):
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    with patch("dashboard.routes.webhooks.cfg", return_value=""), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=_signed_payload())
    assert resp.status_code == 401
    mock_store.assert_not_called()


def test_missing_secret_with_unsigned_allowed_never_stores(client, monkeypatch):
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "1")
    with patch("dashboard.routes.webhooks.cfg", return_value=""), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content='{"id": 1, "text": "hi"}')
    assert resp.status_code == 200
    mock_store.assert_not_called()


def test_oversized_body_is_rejected(client):
    huge_body = b"x" * (64 * 1024 + 1)
    with patch("dashboard.routes.webhooks.cfg", side_effect=_cfg_with_secret()), \
         patch("dashboard.routes.webhooks._store_event") as mock_store:
        resp = client.post(PATH, content=huge_body, headers={"content-length": str(len(huge_body))})
    assert resp.status_code == 413
    mock_store.assert_not_called()


def test_rate_limited_returns_429(client):
    with patch("dashboard.routes.webhooks.cfg", side_effect=lambda key: {"dialpad_webhook_secret": SECRET,
                                                                          "casey_webhook_rate_limit_per_minute": "3"}.get(key, "")), \
         patch("dashboard.routes.webhooks._store_event"):
        token = _signed_payload()
        codes = [client.post(PATH, content=token).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_other_dashboard_routes_still_require_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_webhook_path_bypasses_login():
    client = TestClient(app)
    with patch("dashboard.routes.webhooks.cfg", side_effect=_cfg_with_secret()), \
         patch("dashboard.routes.webhooks._store_event"):
        resp = client.post(PATH, content=_signed_payload(), follow_redirects=False)
    assert resp.status_code != 302


# --- SlidingWindowRateLimiter unit tests (direct, no HTTP) ------------------

def test_rate_limiter_allows_up_to_limit():
    limiter = SlidingWindowRateLimiter(window_seconds=60)
    now = 1000.0
    for _ in range(5):
        assert limiter.allow("1.2.3.4", limit=5, now=now) is True
    assert limiter.allow("1.2.3.4", limit=5, now=now) is False


def test_rate_limiter_resets_after_window():
    limiter = SlidingWindowRateLimiter(window_seconds=60)
    for _ in range(5):
        limiter.allow("1.2.3.4", limit=5, now=1000.0)
    assert limiter.allow("1.2.3.4", limit=5, now=1000.0) is False
    assert limiter.allow("1.2.3.4", limit=5, now=1061.0) is True  # 61s later


def test_rate_limiter_tracks_keys_independently():
    limiter = SlidingWindowRateLimiter(window_seconds=60)
    for _ in range(5):
        limiter.allow("1.2.3.4", limit=5, now=1000.0)
    assert limiter.allow("1.2.3.4", limit=5, now=1000.0) is False
    assert limiter.allow("5.6.7.8", limit=5, now=1000.0) is True
