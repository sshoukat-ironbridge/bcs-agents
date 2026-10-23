import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import socket
import ssl
import time
from typing import Any
from dotenv import load_dotenv
from utils.logger import get_logger
from config.loader import cfg

load_dotenv()
logger = get_logger("integrations.sheets")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetFetchError(Exception):
    """A sheet tab fetch failed (after retries, for retryable errors).

    Distinct from "the tab fetched fine but had no data rows" — callers
    must not treat this the same as an empty tab (e.g. must not retire
    jobs on the strength of a failed fetch).
    """


# Retry only transient/transport failures (SSL errors, timeouts, 5xx) —
# anything else (bad request, auth failure, sheet not found) fails fast.
_RETRY_DELAYS_SECONDS = (2, 5, 10)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return False
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return isinstance(status, int) and status >= 500
    return False


def _load_sa_info() -> dict:
    import os as _os
    value = _os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not value:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if value.startswith("{"):
        return json.loads(value)
    with open(value) as f:
        return json.load(f)


def _get_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_info = _load_sa_info()
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_all_rows(
    spreadsheet_id: str,
    sheet_name: str,
    header_row: int = 1,
    _sleep=time.sleep,
) -> list[dict[str, Any]]:
    """
    Fetch all rows from a sheet tab.
    header_row: 1-based index of the row containing column headers.
    All rows before it are skipped; data rows start immediately after.

    Distinguishes "fetched fine, tab is just empty" (returns []) from
    "fetch failed" (raises SheetFetchError) — callers must not treat a
    failure as an empty tab. Transient errors (SSL, timeout, 5xx) are
    retried up to 3 times with 2s/5s/10s backoff; other errors fail
    immediately without retrying. `_sleep` is only a seam for tests.
    """
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping Sheets fetch")
        return []

    last_exc: "Exception | None" = None
    attempts = len(_RETRY_DELAYS_SECONDS) + 1

    for attempt in range(1, attempts + 1):
        try:
            service = _get_service()
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=sheet_name)
                .execute()
            )
            values = result.get("values", [])
            if not values or len(values) < header_row:
                logger.info(f"Sheet '{sheet_name}' returned no data")
                return []

            headers = values[header_row - 1]          # 0-indexed
            data_rows = values[header_row:]            # everything after header row

            rows = [
                dict(zip(headers, row + [""] * (len(headers) - len(row))))
                for row in data_rows
            ]
            logger.info(f"Fetched {len(rows)} rows from sheet '{sheet_name}' (header_row={header_row})")
            return rows

        except ValueError as e:
            # Config error (e.g. missing/invalid GOOGLE_SERVICE_ACCOUNT_JSON) —
            # not a transient fetch failure, but still a failure: the caller
            # must not treat this as "tab is empty".
            logger.error(f"Sheets config error for '{sheet_name}': {e}")
            raise SheetFetchError(f"Sheets config error for '{sheet_name}': {e}") from e
        except Exception as e:
            last_exc = e
            if not _is_retryable(e):
                logger.error(f"Sheets API error for '{sheet_name}' (not retryable): {e}")
                raise SheetFetchError(f"Failed to fetch sheet tab '{sheet_name}': {e}") from e
            if attempt < attempts:
                delay = _RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    f"Sheets API error for '{sheet_name}' (attempt {attempt}/{attempts}), "
                    f"retrying in {delay}s: {e}"
                )
                _sleep(delay)

    logger.error(f"Sheets API error for '{sheet_name}' after {attempts} attempts: {last_exc}")
    raise SheetFetchError(f"Failed to fetch sheet tab '{sheet_name}' after {attempts} attempts: {last_exc}") from last_exc


PM_NAME_MAP = {
    "lau":       "Laura Peña",
    "laura":     "Laura Peña",
    "laura a":   "Laura Arbelaez",
    "laura.a":   "Laura Arbelaez",
    "julie":     "Julie Martinez",
    "tatiana":   "Tatiana Moreno",
    "tati":      "Tatiana Moreno",
    "gustavo":   "Gustavo Zuluaga",
    "tavo":      "Gustavo Zuluaga",
    "dan":       "Dan Diazgranados",
    "daniel":    "Daniel Gomez Cortez",
    "alfredo":   "Alfredo Núñez",
    "alfred":    "Alfredo Núñez",
    "esteban":   "Esteban Estarita",
    "steve":     "Esteban Estarita",
    "andrea":    "Andrea Ortega",
    "santi":     "Santiago",
    "jordan":    "Jordan Hantman",
    "ryan":      "Ryan Annis",
    "christian": "Christian Audé",
    "karen":     "Karen Garrido",
    "elias":     "Elias Babilonia",
    "lucia":     "Lucia Fuente Buena",
    "yower":     "Yower",
}


def normalize_pm_name(raw_name: str) -> str:
    key = raw_name.strip().lower()
    return PM_NAME_MAP.get(key, raw_name.strip())


def parse_latest_date(cell_value: str) -> "date | None":
    from datetime import date, datetime
    from utils.timeutil import today_ny
    if not cell_value or not cell_value.strip():
        return None

    FORMATS = ["%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%m/%d", "%m-%d"]
    current_year = today_ny().year
    best: date | None = None

    for raw in cell_value.replace("\r", "\n").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        for fmt in FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt)
                if fmt in ("%m/%d", "%m-%d"):
                    parsed = parsed.replace(year=current_year)
                candidate = parsed.date()
                if best is None or candidate > best:
                    best = candidate
                break
            except ValueError:
                continue

    return best


_INVALID_PM_VALUES = {"estimating", "sales"}

# Raw PM cell values (case-insensitive) that indicate an intentional routing/hold state.
# Jobs matching these get pm_name="" and comms_hold=True so all agents skip them silently.
# "HOLD/<anything>" prefix is also matched (e.g. HOLD/Antonio, HOLD/DAN).
# Contrast with _INVALID_PM_VALUES: those are genuinely missing data that still need a human fix.
_HOLD_PM_VALUES = {"construction", "permits", "dan", "tatiana"}


def is_valid_pm(pm_name: str, pm_config: list) -> bool:
    """True if pm_name resolves to a known PM via find_pm().

    Used by OCA's check_missing_pm to flag jobs with no recognised PM, and by
    Casey to decide whether a job has someone to send the customer-update email as.
    """
    from db.state_store import find_pm
    return find_pm(pm_name, pm_config) is not None


def _row_to_job(row: dict, sheet_tab: str) -> "dict[str, Any] | None":
    # Normalize all keys to lowercase+stripped so lookups are case-insensitive.
    # This survives any future casing changes the client makes to the header row.
    r = {k.lower().strip(): v for k, v in row.items()}

    def col(name: str) -> str:
        return r.get(name.lower().strip(), "").strip()

    client = col("client")
    if not client:
        return None

    raw_pm_cell = col("project manager")
    raw_key = raw_pm_cell.lower()

    if raw_key in _HOLD_PM_VALUES or raw_key.startswith("hold/"):
        # Intentional routing/hold marker — not a real PM. Suppress all processing.
        logger.info(f"Row for client '{client}' has hold PM value (raw='{raw_pm_cell}') — setting comms_hold=True")
        pm_name = ""
        comms_hold = True
        comms_hold_reason = f"Sheet PM cell: '{raw_pm_cell}'"
    elif raw_key in _INVALID_PM_VALUES:
        # Non-PM label (e.g. "estimating", "sales") — missing data, not an intentional hold.
        logger.info(f"Row for client '{client}' has invalid PM label (raw='{raw_pm_cell}')")
        pm_name = ""
        comms_hold = False
        comms_hold_reason = None
    elif not raw_key:
        # Genuinely blank — real missing data. Let check_missing_pm flag it; do NOT hold.
        pm_name = ""
        comms_hold = False
        comms_hold_reason = None
    else:
        pm_name = normalize_pm_name(raw_pm_cell)
        comms_hold = False
        comms_hold_reason = None

    # PRIMARY JOB TYPE is the canonical source; fall back to SECONDARY JOB TYPE
    # if primary is blank; combine as "Primary - Secondary" when both are present.
    primary_type = col("primary job type")
    secondary_type = col("secondary job type")
    if primary_type and secondary_type:
        job_type = f"{primary_type} - {secondary_type}"
    else:
        job_type = primary_type or secondary_type

    return {
        "client_name":              client,
        "pm_name":                  pm_name,
        "comms_hold":               comms_hold,
        "comms_hold_reason":        comms_hold_reason,
        "job_type":                 job_type,
        "primary_job_type":         primary_type,
        "start_date":               col("start date"),
        "estimated_start_window":   col("realistic start date"),
        "deposit_date":             col("deposit date"),
        # Raw sheet column — fallback when no Gmail-based contact history exists.
        "sheet_last_contact":       col("most recent communication"),
        # Retained for casey_active_jobs / dashboard display only.
        "last_pm_contact":          (
            parse_latest_date(col("most recent communication"))
            or parse_latest_date(col("pm communication history"))
        ),
        "pm_communication_history": col("pm communication history"),
        "assigned_crew_sub":        col("subcontractor name"),
        "customer_phone":           col("phone number"),
        "deadline_to_start":        col("dead line to start"),
        "projected_end_date":       col("projected end date"),
        "end_date":                 col("end date"),
        "complaint":                col("complaint/notes"),
        "complaint_note":           col("complaint/notes"),
        "permit":                   col("permit"),
        "total_project":            col("total project"),
        "to_collect":               col("to collect"),
        "client_mood":              col("client mood"),
        "pm_notes":                 col("alfred communication/notes"),
        "email":                    col("email"),
        "job_description":          col("scope of work"),
        "estimator_name":           col("estimator"),
        "address":                  col("address"),
        "new_sub":                  col("new sub?"),
        "sheet_tab":                sheet_tab,
    }


def get_active_jobs() -> list[dict[str, Any]]:
    """Raises SheetFetchError (propagated from get_all_rows) if any
    configured tab fails to fetch — callers must not swallow this and
    treat it as "no active jobs"."""
    spreadsheet_id = cfg("google_sheets_id", "GOOGLE_SHEETS_ID")
    if not spreadsheet_id:
        logger.warning("GOOGLE_SHEETS_ID not set — skipping active jobs fetch")
        return []

    jobs: list[dict[str, Any]] = []

    for tab_name, sheet_tab in (("To Start", "to_start"), ("In Process", "in_process")):
        rows = get_all_rows(spreadsheet_id, tab_name, header_row=2)
        if not rows:
            logger.warning(f"Sheet tab '{tab_name}' returned no rows")
            continue
        for row in rows:
            job = _row_to_job(row, sheet_tab)
            if job:
                jobs.append(job)

    logger.info(f"Active jobs found: {len(jobs)}")
    return jobs


def get_jobs_by_client(client_name: str) -> list[dict[str, Any]]:
    jobs = get_active_jobs()
    needle = client_name.strip().lower()
    matches = [j for j in jobs if j["client_name"].lower() == needle]
    logger.info(f"Jobs found for '{client_name}': {len(matches)}")
    return matches


if __name__ == "__main__":
    jobs = get_active_jobs()
    print(f"Found {len(jobs)} active jobs")
    print()
    for job in jobs[:3]:
        for key, value in job.items():
            print(f"  {key}: {value}")
        print()

    print("--- get_jobs_by_client('Mark Parkinson') ---")
    matches = get_jobs_by_client("Mark Parkinson")
    if matches:
        for job in matches:
            for key, value in job.items():
                print(f"  {key}: {value}")
            print()
    else:
        print("  No matches found")
