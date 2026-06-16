"""Tests for sheets.py pure helpers (no Google API calls)."""
import sheets


def test_as_int_parses_sheet_values():
    assert sheets._as_int("220") == 220
    assert sheets._as_int("220.0") == 220
    assert sheets._as_int("") == 0
    assert sheets._as_int(None) == 0


def test_trailing_meal_count_empty():
    assert sheets._trailing_meal_count([]) == 0


def test_trailing_meal_count_single_meal():
    rows = [
        ["1", "2026-06-17", "09:00", "Oatmeal", "200", "290", "9", "7", "48"],
        ["1", "2026-06-17", "09:00", "Coffee", "150", "45", "2", "2", "4"],
    ]
    assert sheets._trailing_meal_count(rows) == 2


def test_trailing_meal_count_only_last_meal():
    rows = [
        ["1", "2026-06-17", "09:00", "Oatmeal", "200", "290", "9", "7", "48"],
        ["2", "2026-06-17", "13:00", "Salad", "150", "85", "2", "6", "7"],
        ["2", "2026-06-17", "13:00", "Bread", "50", "130", "4", "1", "25"],
    ]
    # last meal is meal_no 2 (two rows), not the earlier meal_no 1
    assert sheets._trailing_meal_count(rows) == 2


def test_trailing_meal_count_respects_date_boundary():
    rows = [
        ["3", "2026-06-16", "20:00", "Dinner", "300", "500", "20", "20", "50"],
        ["1", "2026-06-17", "08:00", "Breakfast", "200", "300", "10", "10", "40"],
    ]
    # the last meal is a single row on a new day, despite an earlier meal_no 3
    assert sheets._trailing_meal_count(rows) == 1
