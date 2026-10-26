from utils.logger import get_logger
from utils.outbound import outbound_enabled
from config.loader import cfg
from integrations.slack import send_message

logger = get_logger("oca.alerts")


def _get_urgentmatters_channel() -> str:
    return cfg("slack_urgentmatters_channel") or "urgentmatters"


def _route(message: str, summary: str = "") -> None:
    channel = _get_urgentmatters_channel()
    if not outbound_enabled():
        logger.info(f"OUTBOUND SUPPRESSED (deadline change alert) -> #{channel}: {summary or message.splitlines()[0]}")
        logger.debug(f"OUTBOUND SUPPRESSED (deadline change alert) -> #{channel} full text:\n{message}")
        return
    send_message(channel, message)


def build_deadline_change_message(
    client_name: str,
    pm_name: str,
    old_deadline: str,
    new_deadline: str,
) -> str:
    return (
        f"📅 *Deadline to Start Changed*\n"
        f"Customer: {client_name}\n"
        f"PM: {pm_name or 'Unassigned'}\n"
        f"Previous deadline: {old_deadline}\n"
        f"New deadline: {new_deadline}"
    )


def send_deadline_change_alert(
    client_name: str,
    pm_name: str,
    old_deadline: str,
    new_deadline: str,
) -> None:
    summary = f"Deadline to Start changed for {client_name}: {old_deadline} -> {new_deadline}"
    _route(build_deadline_change_message(client_name, pm_name, old_deadline, new_deadline), summary=summary)
