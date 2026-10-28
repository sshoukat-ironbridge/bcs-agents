"""One-off verification script for Stage 9: hits every new Casey dashboard
route with FastAPI's TestClient as an admin user and prints status codes,
and confirms an unauthenticated request redirects to login. Not part of
the permanent test suite — run manually:

    docker compose run --rm -v "$(pwd):/app" oca python scripts/verify_casey_dashboard.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.main import app  # noqa: E402

ADMIN_USER = {"id": 1, "email": "admin@example.com", "role": "admin", "linked_name": ""}


def main() -> None:
    client = TestClient(app)

    print("Unauthenticated:")
    resp = client.get("/casey", follow_redirects=False)
    print(f"  GET /casey -> {resp.status_code} (expect 302/401)")

    with patch("dashboard.auth.get_current_user", return_value=ADMIN_USER):
        print("As admin:")
        for path in ("/casey", "/casey/flags", "/casey/flags?severity=High", "/casey/flags?page=1"):
            resp = client.get(path)
            print(f"  GET {path} -> {resp.status_code}")

        resp = client.get("/casey/flags")
        import re
        match = re.search(r"/casey/flags/(\d+)", resp.text)
        if match:
            flag_id = match.group(1)
            resp = client.get(f"/casey/flags/{flag_id}")
            print(f"  GET /casey/flags/{flag_id} -> {resp.status_code}")
            resp = client.get(f"/casey/flags/{flag_id}", headers={"hx-request": "true"})
            print(f"  GET /casey/flags (hx-request) -> {resp.status_code}")
        else:
            print("  (no flag id found in tracker page — did you seed demo data?)")


if __name__ == "__main__":
    main()
