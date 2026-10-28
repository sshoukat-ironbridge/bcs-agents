"""Creates two local demo dashboard logins for exercising Casey's
role-based visibility: a PM user linked to one real PM name, and a
leadership user (full visibility). Prints the generated emails and
passwords — nowhere else are they stored in plaintext.

Usage:
    docker compose run --rm oca python scripts/seed_casey_demo_users.py [--reset]
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("casey.seed_demo_users")

PM_USER_EMAIL = "demo-pm@example-demo.com"
LEADERSHIP_USER_EMAIL = "demo-leadership@example-demo.com"


def _generate_password() -> str:
    return secrets.token_urlsafe(12)


def run(reset: bool = False) -> dict:
    from dashboard.auth import hash_password
    from db.state_store import get_pm_list, get_user_by_email, create_user, delete_user

    pms = get_pm_list()
    if not pms:
        return {"ok": False, "error": "no PMs in pm_config — nothing to link the demo PM user to"}
    pm_name = sorted(p["full_name"] for p in pms)[0]

    created = []
    for email, role, linked_name in (
        (PM_USER_EMAIL, "pm", pm_name),
        (LEADERSHIP_USER_EMAIL, "leadership", ""),
    ):
        existing = get_user_by_email(email)
        if existing and not reset:
            created.append({"email": email, "role": role, "linked_name": linked_name, "password": None, "existed": True})
            continue
        if existing and reset:
            delete_user(email)
        password = _generate_password()
        user = create_user(email, hash_password(password), role=role, linked_name=linked_name)
        if not user:
            return {"ok": False, "error": f"failed to create {email}"}
        created.append({"email": email, "role": role, "linked_name": linked_name, "password": password, "existed": False})

    return {"ok": True, "users": created, "pm_name": pm_name}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="Delete existing demo users first, then recreate with new passwords.")
    args = parser.parse_args()

    result = run(reset=args.reset)
    if not result.get("ok"):
        print(f"ERROR: {result.get('error')}", file=sys.stderr)
        sys.exit(1)

    print(f"Linked PM: {result['pm_name']}\n")
    for u in result["users"]:
        if u["existed"]:
            print(f"{u['role']:<12} {u['email']:<32} (already exists — pass --reset for a new password)")
        else:
            print(f"{u['role']:<12} {u['email']:<32} password: {u['password']}")


if __name__ == "__main__":
    main()
