"""Tests for sheets.py pure helpers (no Google API calls)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import sheets


def _now(hour, minute):
    return datetime(2026, 6, 17, hour, minute, tzinfo=ZoneInfo("UTC"))


def test_as_int_parses_sheet_values():
    assert sheets._as_int("220") == 220
    assert sheets._as_int("220.0") == 220
    assert sheets._as_int("") == 0
    assert sheets._as_int(None) == 0


def test_trailing_entry_count_empty():
    assert sheets._trailing_entry_count([]) == 0


def test_trailing_entry_count_groups_one_append():
    # One append (e.g. a photo with two dishes) shares a single date+time.
    rows = [
        ["1", "2026-06-17", "09:00", "Oatmeal", "200", "290", "9", "7", "48"],
        ["1", "2026-06-17", "09:00", "Coffee", "150", "45", "2", "2", "4"],
    ]
    assert sheets._trailing_entry_count(rows) == 2


def test_trailing_entry_count_splits_same_meal_different_time():
    # Same meal_no (one meal, logged in two messages) but different times: undo must
    # only remove the LAST entry, not the whole meal.
    rows = [
        ["1", "2026-06-17", "09:00", "Bread", "50", "109", "3", "1", "22"],
        ["1", "2026-06-17", "09:30", "Pate", "20", "62", "3", "5", "1"],
    ]
    assert sheets._trailing_entry_count(rows) == 1


def test_next_meal_no_first_of_day():
    assert sheets._next_meal_no([], _now(8, 0)) == 1


def test_next_meal_no_continues_within_window():
    rows = [["2", "2026-06-17", "12:00", "Salad", "150", "85", "2", "6", "7"]]
    assert sheets._next_meal_no(rows, _now(12, 30)) == 2  # 30 min later → same meal (< 37)


def test_next_meal_no_new_after_window():
    rows = [["2", "2026-06-17", "12:00", "Salad", "150", "85", "2", "6", "7"]]
    assert sheets._next_meal_no(rows, _now(12, 40)) == 3  # 40 min later → new meal (> 37)
