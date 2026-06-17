"""Tests for handler.py routing/formatting helpers (no Telegram/AWS calls)."""
import handler

RESULT = {
    "items": [
        {"name": "Banana", "amount_g": 120, "calories": 105, "protein": 1, "fat": 0, "carbs": 27},
    ],
    "total": {"calories": 105, "protein": 1, "fat": 0, "carbs": 27},
}


def test_intent_mode_defaults_to_log():
    assert handler._intent_mode({}) == "log"
    assert handler._intent_mode({"intent": "banana"}) == "log"


def test_intent_mode_passes_known_intents():
    for intent in ("estimate", "chat", "undo", "correct"):
        assert handler._intent_mode({"intent": intent}) == intent


def test_as_int_is_lenient():
    assert handler._as_int("105") == 105
    assert handler._as_int("105.6") == 105
    assert handler._as_int(None) == 0
    assert handler._as_int("n/a") == 0


def test_model_choice_parses_known_key():
    mid, label = handler._model_choice("model:haiku")
    assert mid == "claude-haiku-4-5"
    assert "Haiku" in label


def test_model_choice_rejects_unknown():
    assert handler._model_choice("model:bogus") is None
    assert handler._model_choice("noise") is None
    assert handler._model_choice("") is None


def test_strip_command():
    assert handler._strip_command("/calc two eggs") == "two eggs"
    assert handler._strip_command("/calc") == ""
    assert handler._strip_command("/remember  borscht ") == "borscht"


def test_format_logged_with_goal_shows_remaining():
    user = {"daily_calorie_goal": 2000}
    out = handler._format_logged(RESULT, day_total=105, user=user)
    assert "Banana — 105 kcal (P1 F0 C27)" in out
    assert "Logged: 105 kcal" in out
    assert "Today: 105 / 2000 kcal · 1895 left" in out


def test_format_logged_without_goal_shows_total_only():
    out = handler._format_logged(RESULT, day_total=105, user={})
    assert "Today: 105 kcal" in out
    assert "left" not in out


def test_format_logged_correct_label():
    out = handler._format_logged(RESULT, day_total=105, user={}, label="Updated")
    assert "Updated: 105 kcal" in out


def test_format_estimate_marks_not_logged():
    out = handler._format_estimate(RESULT)
    assert out.startswith("Estimate (not logged):")
    assert "Total: 105 kcal" in out


def test_format_estimate_handles_empty():
    assert handler._format_estimate({"items": []}) == "Couldn't estimate that."
