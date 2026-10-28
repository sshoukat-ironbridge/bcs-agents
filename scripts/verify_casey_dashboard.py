"""One-off verification script for Stage 9: hits every Casey dashboard
route with FastAPI's TestClient as an admin user AND as a PM user, and
prints status codes; confirms an unauthenticated request redirects to
login. Not part of the permanent test suite — run manually:

    docker compose run --rm -v "$(pwd):/app" oca python scripts/verify_casey_dashboard.py
"""
import re
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.main import app  # noqa: E402

ADMIN_USER = {"id": 1, "email": "admin@example.com", "role": "admin", "linked_name": ""}

ALL_ROUTES = (
    "/casey", "/casey/flags", "/casey/flags?severity=High", "/casey/flags?page=1",
    "/casey/people", "/casey/people?type=estimator", "/casey/positive",
    "/casey/responses", "/casey/trends", "/casey/trends?weeks=4", "/casey/trends?weeks=12",
)


def _first_pm_name() -> str:
    from db.state_store import get_pm_list
    pms = sorted(get_pm_list(), key=lambda p: p["full_name"])
    return pms[0]["full_name"] if pms else "Demo PM"


def main() -> None:
    client = TestClient(app)

    print("Unauthenticated:")
    resp = client.get("/casey", follow_redirects=False)
    print(f"  GET /casey -> {resp.status_code} (expect 302/401)")

    with patch("dashboard.auth.get_current_user", return_value=ADMIN_USER):
        print("\nAs admin:")
        for path in ALL_ROUTES:
            resp = client.get(path)
            print(f"  GET {path} -> {resp.status_code}")

        resp = client.get("/casey/flags")
        match = re.search(r"/casey/flags/(\d+)", resp.text)
        if match:
            flag_id = match.group(1)
            resp = client.get(f"/casey/flags/{flag_id}")
            print(f"  GET /casey/flags/{flag_id} -> {resp.status_code}")
            resp = client.get("/casey/flags", headers={"hx-request": "true"})
            print(f"  GET /casey/flags (hx-request) -> {resp.status_code}")
        else:
            print("  (no flag id found in tracker page — did you seed demo data?)")

    pm_name = _first_pm_name()
    pm_user = {"id": 2, "email": "pm@example.com", "role": "pm", "linked_name": pm_name}
    with patch("dashboard.auth.get_current_user", return_value=pm_user):
        print(f"\nAs PM ({pm_name}):")
        for path in ALL_ROUTES:
            resp = client.get(path)
            print(f"  GET {path} -> {resp.status_code}")


if __name__ == "__main__":
    main()
