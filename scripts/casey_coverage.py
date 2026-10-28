"""Coverage report: which active jobs Casey actually monitors today, and
why the rest are excluded. READ ONLY — this never writes anything, and
a failing mailbox check never crashes the report.

Usage:
    docker compose run --rm casey python scripts/casey_coverage.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

logger = get_logger("casey.coverage")


def compute_coverage() -> dict:
    from db.state_store import get_all_active_jobs, get_pm_list, find_pm

    jobs = [j for j in get_all_active_jobs() if not j.get("retired")]
    pm_list = get_pm_list()

    monitored_by_pm: dict[str, int] = {}
    monitored_missing_dialpad: dict[str, int] = {}
    on_hold = 0
    blank_pm = 0
    unrecognized: dict[str, dict] = {}  # raw_name -> {"count", "examples"}
    missing_email: dict[str, int] = {}

    for job in jobs:
        if job.get("comms_hold"):
            on_hold += 1
            continue

        raw_pm = (job.get("pm_name") or "").strip()
        if not raw_pm:
            blank_pm += 1
            continue

        pm = find_pm(raw_pm, pm_list)
        if not pm:
            entry = unrecognized.setdefault(raw_pm, {"count": 0, "examples": []})
            entry["count"] += 1
            if len(entry["examples"]) < 3:
                entry["examples"].append(job.get("client_name", ""))
            continue

        if not (pm.get("email") or "").strip():
            missing_email[pm["full_name"]] = missing_email.get(pm["full_name"], 0) + 1
            continue

        canonical = pm["full_name"]
        monitored_by_pm[canonical] = monitored_by_pm.get(canonical, 0) + 1
        if not (pm.get("dialpad_user_id") or "").strip():
            monitored_missing_dialpad[canonical] = monitored_missing_dialpad.get(canonical, 0) + 1

    return {
        "total_active_jobs": len(jobs),
        "monitored_by_pm": monitored_by_pm,
        "monitored_total": sum(monitored_by_pm.values()),
        "monitored_missing_dialpad": monitored_missing_dialpad,
        "not_monitored": {
            "on_hold": on_hold,
            "blank_pm": blank_pm,
            "unrecognized_pm_name": unrecognized,
            "missing_email": missing_email,
        },
    }


def check_mailboxes() -> dict:
    from db.state_store import get_pm_list
    from integrations.gmail_read import check_mailbox

    results = {}
    for pm in get_pm_list():
        email = (pm.get("email") or "").strip()
        if not email:
            continue
        try:
            ok, error = check_mailbox(email)
        except Exception as e:  # defensive: check_mailbox itself never raises, but be certain
            ok, error = False, str(e)
        results[email] = {"pm_name": pm["full_name"], "ok": ok, "error": error}
    return results


def print_report() -> None:
    coverage = compute_coverage()

    print(f"Total active (non-retired) jobs: {coverage['total_active_jobs']}")
    print(f"\nMonitored jobs by PM (active, not on hold, PM resolved with an email):")
    for pm_name, count in sorted(coverage["monitored_by_pm"].items()):
        flag = " (no dialpad_user_id — email only)" if pm_name in coverage["monitored_missing_dialpad"] else ""
        print(f"  {pm_name:<15} {count}{flag}")
    print(f"  TOTAL MONITORED: {coverage['monitored_total']}")

    nm = coverage["not_monitored"]
    print(f"\nNot monitored:")
    print(f"  On comms hold: {nm['on_hold']}")
    print(f"  Blank PM: {nm['blank_pm']}")
    print(f"  Unrecognized PM name:")
    for name, entry in sorted(nm["unrecognized_pm_name"].items(), key=lambda kv: -kv[1]["count"]):
        print(f"    {name!r}: {entry['count']} job(s), e.g. {entry['examples']}")
    print(f"  PM row missing email:")
    for pm_name, count in sorted(nm["missing_email"].items()):
        print(f"    {pm_name}: {count}")

    print("\nMailbox check (messages.list maxResults=1, impersonated via the service account):")
    for email, result in check_mailboxes().items():
        status = "OK" if result["ok"] else f"FAILED: {result['error']}"
        print(f"  {result['pm_name']:<15} <{email}>: {status}")


if __name__ == "__main__":
    print_report()
