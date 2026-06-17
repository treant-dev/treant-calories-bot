"""Tests for claude.py parsing and context-block formatting (no API calls)."""
import base64
import io
import types

from PIL import Image

import claude


def _resp(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])


def _jpeg(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 200, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _decode(data):
    return Image.open(io.BytesIO(base64.standard_b64decode(data)))


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


def test_parse_extracts_json_after_reasoning():
    # The model prepended chain-of-thought before the JSON; we must not leak it.
    text = (
        "I can see wafers and apple slices on the scale. Let me ask about the wafers.\n\n"
        '{"intent": "log", "needs_clarification": true, "question": "На тарелке вафли?"}'
    )
    out = claude._parse(_resp(text))
    assert out["intent"] == "log"
    assert out["needs_clarification"] is True
    assert out["question"] == "На тарелке вафли?"
    assert "I can see" not in str(out)


def test_parse_ignores_trailing_prose():
    out = claude._parse(_resp('{"intent": "estimate", "items": []}\n\nLet me know if that helps!'))
    assert out == {"intent": "estimate", "items": []}


def test_parse_handles_braces_in_string_values():
    out = claude._parse(_resp('reasoning... {"intent": "chat", "reply": "use {sugar} sparingly"}'))
    assert out["reply"] == "use {sugar} sparingly"


def test_foods_block_lists_each_food():
    block = claude._foods_block([
        {"food_name": "oatmeal", "calories": 290, "protein": 9, "fat": 7, "carbs": 48},
    ])
    assert "oatmeal" in block
    assert "290 kcal" in block
    assert "P9 F7 C48" in block


def test_encode_image_downscales_large_photo():
    data, media_type = claude._encode_image(_jpeg(2000, 1500), "image/jpeg")
    assert media_type == "image/jpeg"
    assert max(_decode(data).size) == claude._MAX_IMAGE_EDGE  # long edge clamped to 768


def test_encode_image_keeps_small_photo():
    data, _ = claude._encode_image(_jpeg(400, 300), "image/jpeg")
    assert _decode(data).size == (400, 300)  # already under the cap → unchanged


def test_encode_image_falls_back_on_non_image():
    raw = b"not an image"
    data, media_type = claude._encode_image(raw, "image/png")
    assert base64.standard_b64decode(data) == raw  # original bytes, untouched
    assert media_type == "image/png"


def test_recent_block_labels_roles():
    block = claude._recent_block([
        {"role": "user", "text": "banana"},
        {"role": "assistant", "text": "Logged: 105 kcal"},
    ])
    assert "User: banana" in block
    assert "Bot: Logged: 105 kcal" in block
