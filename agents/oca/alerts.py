from utils.logger import get_logger
from config.loader import cfg
from integrations.slack import send_message

logger = get_logger("oca.alerts")


def _get_urgentmatters_channel() -> str:
    return cfg("slack_urgentmatters_channel") or "urgentmatters"


def _route(message: str) -> None:
    send_message(_get_urgentmatters_channel(), message)


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
    _route(build_deadline_change_message(client_name, pm_name, old_deadline, new_deadline))
