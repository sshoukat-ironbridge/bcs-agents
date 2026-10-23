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


def run() -> None:
    # AGENT_RUN_ID is set by the dashboard when it spawns this as a subprocess
    # (it already created the row and owns the live SSE log stream + final
    # status/finished_at) — reuse that row instead of creating a second one.
    # Cron/CLI invocations have no such env var, so they create their own.
    external_run_id = os.getenv("AGENT_RUN_ID")
    own_run = external_run_id is None
    run_id = int(external_run_id) if external_run_id else create_agent_run("casey")

    try:
        summary = _run(run_id)
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


def _run(run_id: "int | None" = None) -> str:
    logger.info("Casey skeleton: no work implemented yet")
    return "Casey skeleton: no work implemented yet"


if __name__ == "__main__":
    run()
