"""Google Sheets logging via the REST API, authed with the service account."""
import json
import os
from datetime import datetime, timedelta
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
_MEAL_WINDOW = timedelta(minutes=37)  # entries within this gap share a meal_no

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


def append_meal(spreadsheet_id, items, meal_no, date, time_str):
    """Append one meal (one row per item). meal_no/date come from the caller — no read.
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


def day_state(spreadsheet_id, date, tz="UTC"):
    """One read of today's rows → (next_meal_no, total_calories, items).

    Everything is derived from the sheet (the source of truth): the total is the
    sum of today's `calories`, `items` is [(name, calories), ...], and next_meal_no
    continues the last meal if the previous entry was logged within an hour else
    starts a new one. Computing the number from the sheet means it starts at 1,
    reuses a number freed by undoing/correcting the last entry, and is never burned
    by a failed append."""
    todays = [x for x in _read_rows(spreadsheet_id) if len(x) > 5 and x[1] == date]
    items = [(x[3], _as_int(x[5])) for x in todays]
    next_no = _next_meal_no(todays, datetime.now(ZoneInfo(tz)))
    return next_no, sum(c for _, c in items), items


def _next_meal_no(todays, now):
    """meal_no for a new entry logged at `now`, given today's rows (chronological)."""
    if not todays:
        return 1
    last = todays[-1]
    last_no = _as_int(last[0])
    try:
        last_dt = datetime.strptime(f"{last[1]} {last[2]}", "%Y-%m-%d %H:%M").replace(
            tzinfo=now.tzinfo)
        if now - last_dt <= _MEAL_WINDOW:
            return last_no                  # within the window → same meal
    except (ValueError, IndexError):
        pass
    return last_no + 1


def _col(row, i):
    """Safe integer read of column `i` (rows may be short when trailing cells are blank)."""
    return _as_int(row[i]) if len(row) > i else 0


def day_totals(spreadsheet_id, date):
    """Return ({calories, protein, fat, carbs}, row_count) summed over `date`."""
    todays = [x for x in _read_rows(spreadsheet_id) if len(x) > 5 and x[1] == date]
    totals = {
        "calories": sum(_col(x, 5) for x in todays),
        "protein": sum(_col(x, 6) for x in todays),
        "fat": sum(_col(x, 7) for x in todays),
        "carbs": sum(_col(x, 8) for x in todays),
    }
    return totals, len(todays)


def _log_sheet_id(spreadsheet_id):
    r = httpx.get(f"{_BASE}/{spreadsheet_id}", params={"fields": "sheets.properties"},
                  headers=_hdr(), timeout=_TIMEOUT)
    r.raise_for_status()
    for s in r.json().get("sheets", []):
        if s["properties"]["title"] == _SHEET:
            return s["properties"]["sheetId"]
    raise RuntimeError("Log sheet not found")


def _trailing_entry_count(rows):
    """How many trailing rows belong to the last logged entry — the rows written by
    the most recent append, identified by a shared (date, time). meal_no can span
    several appends (a whole meal), so it can't be used here."""
    if not rows:
        return 0
    last = rows[-1]
    last_date = last[1] if len(last) > 1 else None
    last_time = last[2] if len(last) > 2 else None
    k = 0
    for r in reversed(rows):
        if len(r) > 2 and r[1] == last_date and r[2] == last_time:
            k += 1
        else:
            break
    return k


def delete_last_meal(spreadsheet_id):
    """Delete the rows of the most recently logged entry (the last append, grouped by
    a shared date+time). Returns [(name, cal), ...] or None."""
    rows = _read_rows(spreadsheet_id)
    k = _trailing_entry_count(rows)
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
