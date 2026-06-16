"""Tests for claude.py parsing and context-block formatting (no API calls)."""
import types

import claude


def _resp(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])


def test_parse_plain_json():
    out = claude._parse(_resp('{"intent": "log", "items": []}'))
    assert out == {"intent": "log", "items": []}


def test_parse_strips_json_fence():
    out = claude._parse(_resp('```json\n{"intent": "estimate"}\n```'))
    assert out["intent"] == "estimate"


def test_parse_prose_falls_back_to_chat():
    out = claude._parse(_resp("I logged it once, not twice."))
    assert out["intent"] == "chat"
    assert "once" in out["reply"]


def test_foods_block_lists_each_food():
    block = claude._foods_block([
        {"food_name": "oatmeal", "calories": 290, "protein": 9, "fat": 7, "carbs": 48},
    ])
    assert "oatmeal" in block
    assert "290 kcal" in block
    assert "P9 F7 C48" in block


def test_recent_block_labels_roles():
    block = claude._recent_block([
        {"role": "user", "text": "banana"},
        {"role": "assistant", "text": "Logged: 105 kcal"},
    ])
    assert "User: banana" in block
    assert "Bot: Logged: 105 kcal" in block
