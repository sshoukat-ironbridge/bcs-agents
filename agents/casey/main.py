import argparse
import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv
from integrations.slack import send_message
from db.state_store import (
    create_agent_run,
    finish_agent_run,
    set_agent_run_summary,
)
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("casey")

# ingest_dialpad/ingest_gmail each take their own lookback window per
# scheduled run; the overlap built into their cursors means a short
# window never misses anything between runs.
STAGE_INGEST_WINDOW = "1d"
ANALYZE_LIMIT_PER_RUN = 10

ALL_STAGES = ["ingest_dialpad", "ingest_gmail", "analyze", "process_flags", "run_watches"]


def _stage_enabled(stage: str) -> bool:
    """Each ingestion/analysis stage is behind its own config flag,
    checked only by this scheduled-run orchestration — a direct CLI
    invocation of that stage's own module always runs regardless."""
    from db.state_store import get_config

    if stage == "ingest_dialpad":
        return (get_config("casey_dialpad_enabled") or "false").strip().lower() == "true"
    if stage == "ingest_gmail":
        return (get_config("casey_gmail_enabled") or "false").strip().lower() == "true"
    if stage == "analyze":
        return (get_config("casey_analysis_enabled") or "false").strip().lower() == "true"
    return True  # process_flags, run_watches: no on/off switch, always run


def _run_stage(stage: str, dry_run: bool) -> dict:
    """Runs one stage in its own try/except so one stage's failure never
    blocks the rest — logged clearly either way."""
    from agents.casey.ingest_windows import parse_since

    try:
        if stage == "ingest_dialpad":
            from agents.casey.ingest_dialpad import run as ingest_dialpad_run
            stats = ingest_dialpad_run(parse_since(STAGE_INGEST_WINDOW), dry_run=dry_run)
        elif stage == "ingest_gmail":
            from agents.casey.ingest_gmail import run as ingest_gmail_run
            stats = ingest_gmail_run(parse_since(STAGE_INGEST_WINDOW), dry_run=dry_run)
        elif stage == "analyze":
            from agents.casey.analyze import run as analyze_run
            stats = analyze_run(limit=ANALYZE_LIMIT_PER_RUN, only_matched=True, dry_run=dry_run)
        elif stage == "process_flags":
            from agents.casey.process_flags import run as process_flags_run
            stats = process_flags_run(dry_run=dry_run)
        elif stage == "run_watches":
            from agents.casey.run_watches import run as run_watches_run
            stats = run_watches_run(dry_run=dry_run)
        else:
            return {"ok": False, "error": f"unknown stage {stage!r}"}
    except Exception as e:
        logger.exception(f"Casey stage {stage!r} failed")
        return {"ok": False, "error": str(e)}

    logger.info(f"Casey stage {stage!r} complete: {stats}")
    return {"ok": True, "stats": stats}


def run_stages(dry_run: bool = False, stages: "list[str] | None" = None) -> dict[str, dict]:
    """Runs the requested stages (default: all, in ALL_STAGES order),
    each independently. Returns {stage: {"ok", "stats"|"error"|"skipped"}}."""
    results: dict[str, dict] = {}
    for stage in stages or ALL_STAGES:
        if stage not in ALL_STAGES:
            logger.warning(f"Unknown Casey stage {stage!r}, skipping")
            continue
        if not _stage_enabled(stage):
            logger.info(f"Casey stage {stage!r} is disabled (config), skipping")
            results[stage] = {"ok": True, "skipped": True}
            continue
        logger.info(f"Casey stage {stage!r} starting (dry_run={dry_run})")
        results[stage] = _run_stage(stage, dry_run)
    return results


def _format_summary(results: dict[str, dict]) -> str:
    parts = []
    for stage, result in results.items():
        if result.get("skipped"):
            parts.append(f"{stage}: skipped")
        elif result["ok"]:
            parts.append(f"{stage}: ok {result.get('stats', {})}")
        else:
            parts.append(f"{stage}: FAILED ({result.get('error')})")
    return "; ".join(parts)


def _run(run_id: "int | None" = None, dry_run: bool = False, stages: "list[str] | None" = None) -> str:
    results = run_stages(dry_run=dry_run, stages=stages)
    summary = _format_summary(results)
    logger.info(f"Casey run complete: {summary}")
    return summary


def run(dry_run: bool = False, stages: "list[str] | None" = None) -> None:
    # AGENT_RUN_ID is set by the dashboard when it spawns this as a subprocess
    # (it already created the row and owns the live SSE log stream + final
    # status/finished_at) — reuse that row instead of creating a second one.
    # Cron/CLI invocations have no such env var, so they create their own.
    external_run_id = os.getenv("AGENT_RUN_ID")
    own_run = external_run_id is None
    run_id = int(external_run_id) if external_run_id else create_agent_run("casey")

    try:
        summary = _run(run_id, dry_run=dry_run, stages=stages)
        set_agent_run_summary(run_id, summary)
        if own_run:
            finish_agent_run(run_id, "success", summary)
    except Exception as e:
        logger.exception("Casey crashed")
        set_agent_run_summary(run_id, f"Error: {e}")
        if own_run:
            finish_agent_run(run_id, "error", f"Error: {e}")
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            msg = (
                f"🔴 Agent crash — Casey failed at {timestamp}\n"
                f"Error: {e}\n"
                f"The next scheduled run will retry automatically."
            )
            urgentmatters = cfg("slack_urgentmatters_channel") or "urgentmatters"
            send_message(urgentmatters, msg)
        except Exception as notify_err:
            print(f"Casey: failed to send crash notification: {notify_err}", file=sys.stderr)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="No DB writes, no API calls that cost money, no sends.")
    parser.add_argument("--stages", help=f"Comma-separated subset of: {','.join(ALL_STAGES)}")
    args = parser.parse_args()

    stages = [s.strip() for s in args.stages.split(",")] if args.stages else None
    run(dry_run=args.dry_run, stages=stages)


if __name__ == "__main__":
    main()
