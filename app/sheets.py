"""Google Sheets logging via the REST API, authed with the service account."""
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from secret_store import get_secret

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_SHEET = "Log"
_HEADERS = ["meal_no", "date", "time", "item", "amount_g",
            "calories", "protein", "fat", "carbs"]
_TIMEOUT = 15

_creds = None


def _sa_info():
    return json.loads(get_secret("google_service_account"))


def service_account_email():
    return _sa_info()["client_email"]


def _token():
    global _creds
    if _creds is None:
        _creds = service_account.Credentials.from_service_account_info(
            _sa_info(), scopes=_SCOPES)
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


def _hdr():
    return {"Authorization": f"Bearer {_token()}"}


# ── onboarding ────────────────────────────────────────────────
def validate_access(spreadsheet_id):
    """Raise if the service account cannot read the spreadsheet."""
    httpx.get(f"{_BASE}/{spreadsheet_id}", headers=_hdr(), timeout=_TIMEOUT).raise_for_status()


def ensure_log_sheet(spreadsheet_id):
    """Create the 'Log' tab (if missing) and write the header row (if empty)."""
    add = httpx.post(
        f"{_BASE}/{spreadsheet_id}:batchUpdate",
        headers=_hdr(),
        json={"requests": [{"addSheet": {"properties": {"title": _SHEET}}}]},
        timeout=_TIMEOUT,
    )
    if not (add.status_code == 400 and "already exists" in add.text):
        add.raise_for_status()

    have = httpx.get(f"{_BASE}/{spreadsheet_id}/values/{_SHEET}!A1:I1",
                     headers=_hdr(), timeout=_TIMEOUT)
    have.raise_for_status()
    if not have.json().get("values"):
        httpx.put(
            f"{_BASE}/{spreadsheet_id}/values/{_SHEET}!A1",
            headers=_hdr(),
            params={"valueInputOption": "RAW"},
            json={"values": [_HEADERS]},
            timeout=_TIMEOUT,
        ).raise_for_status()


# ── logging ───────────────────────────────────────────────────
def _read_rows(spreadsheet_id):
    r = httpx.get(f"{_BASE}/{spreadsheet_id}/values/{_SHEET}!A2:I",
                  headers=_hdr(), timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json().get("values", [])


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def now_parts(tz="UTC"):
    n = datetime.now(ZoneInfo(tz))
    return n.strftime("%Y-%m-%d"), n.strftime("%H:%M")


_ROW_RE = re.compile(r"![A-Z]+(\d+)")


def range_start_row(a1_range):
    """First row number of an A1 range like 'Log!A42:I43' → 42 (or None)."""
    m = _ROW_RE.search(a1_range or "")
    return int(m.group(1)) if m else None


def append_meal(spreadsheet_id, items, meal_no, date, time_str):
    """Append one meal (one row per item). meal_no/date come from the caller — no read.
    Returns the written A1 range (e.g. 'Log!A42:I43') so the caller can record it.
    If the Log tab was deleted, recreate it and retry once."""
    new_rows = [
        [meal_no, date, time_str, it["name"], it.get("amount_g", ""),
         it["calories"], it["protein"], it["fat"], it["carbs"]]
        for it in items
    ]
    try:
        return _append(spreadsheet_id, new_rows)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 400:
            raise
        ensure_log_sheet(spreadsheet_id)   # tab/headers gone → recreate, then retry
        return _append(spreadsheet_id, new_rows)


def _append(spreadsheet_id, rows):
    resp = httpx.post(
        f"{_BASE}/{spreadsheet_id}/values/{_SHEET}!A1:append",
        headers=_hdr(),
        params={"valueInputOption": "RAW"},
        json={"values": rows},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("updates", {}).get("updatedRange")


def day_summary(spreadsheet_id, date, start_row=None):
    """Return (total_calories, [(name, calories), ...]) for `date`.
    Reads only Log!A{start_row}:I when start_row is known; falls back to a full read
    (and re-filters) if that bounded range doesn't contain the date — e.g. after a
    manual edit shifted the rows."""
    rng = f"{_SHEET}!A{int(start_row)}:I" if start_row else f"{_SHEET}!A2:I"
    r = httpx.get(f"{_BASE}/{spreadsheet_id}/values/{rng}", headers=_hdr(), timeout=_TIMEOUT)
    r.raise_for_status()
    rows = r.json().get("values", [])
    todays = [x for x in rows if len(x) > 5 and x[1] == date]
    if start_row and not todays:                     # stale pointer → self-heal
        todays = [x for x in _read_rows(spreadsheet_id) if len(x) > 5 and x[1] == date]
    items = [(x[3], _as_int(x[5])) for x in todays]
    return sum(c for _, c in items), items


def _log_sheet_id(spreadsheet_id):
    r = httpx.get(f"{_BASE}/{spreadsheet_id}", params={"fields": "sheets.properties"},
                  headers=_hdr(), timeout=_TIMEOUT)
    r.raise_for_status()
    for s in r.json().get("sheets", []):
        if s["properties"]["title"] == _SHEET:
            return s["properties"]["sheetId"]
    raise RuntimeError("Log sheet not found")


def _trailing_meal_count(rows):
    """How many trailing rows belong to the last logged meal (same meal_no + date)."""
    if not rows:
        return 0
    last_no = rows[-1][0]
    last_date = rows[-1][1] if len(rows[-1]) > 1 else None
    k = 0
    for r in reversed(rows):
        if r and r[0] == last_no and len(r) > 1 and r[1] == last_date:
            k += 1
        else:
            break
    return k


def delete_last_meal(spreadsheet_id):
    """Delete the rows of the most recently logged meal. Returns [(name, cal), ...] or None."""
    rows = _read_rows(spreadsheet_id)
    k = _trailing_meal_count(rows)
    if not k:
        return None
    n = len(rows)
    httpx.post(
        f"{_BASE}/{spreadsheet_id}:batchUpdate",
        headers=_hdr(),
        json={"requests": [{"deleteDimension": {"range": {
            "sheetId": _log_sheet_id(spreadsheet_id),
            "dimension": "ROWS",
            "startIndex": 1 + (n - k),   # +1 skips the header row
            "endIndex": 1 + n,
        }}}]},
        timeout=_TIMEOUT,
    ).raise_for_status()
    return [(r[3], _as_int(r[5])) for r in rows[-k:] if len(r) > 5]
