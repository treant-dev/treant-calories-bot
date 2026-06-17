"""Food analysis via the Claude API."""
import base64
import io
import json
import logging
import os

from anthropic import Anthropic
from PIL import Image

from prompts import CLARIFY_PROMPT, SYSTEM_PROMPT
from secret_store import get_secret

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
_MAX_IMAGE_EDGE = 768  # downscale before sending — cuts image-token cost ~3x
_client = None


def _client_():
    # Lazily built and reused across warm invocations.
    global _client
    if _client is None:
        _client = Anthropic(api_key=get_secret("anthropic_api_key"))
    return _client


def default_model():
    """The model used when a user hasn't picked one (the ANTHROPIC_MODEL env default)."""
    return _MODEL


def analyze_text(description, known_foods=None, recent=None, model=None):
    """Analyze a text meal description. Returns the parsed JSON dict."""
    return _ask(description, known_foods, recent, model)


def analyze_image(image_bytes, media_type="image/jpeg", caption=None,
                  known_foods=None, recent=None, model=None):
    """Analyze a food photo (optionally with the user's caption)."""
    content = [
        _image_block(image_bytes, media_type),
        {"type": "text", "text": caption
         or "Food photo — could be a new meal or more detail on the last one."},
    ]
    return _ask(content, known_foods, recent, model)


def _image_block(image_bytes, media_type="image/jpeg"):
    """Build a base64 image content block, downscaling large photos first."""
    data, mt = _encode_image(image_bytes, media_type)
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}


def _encode_image(image_bytes, media_type):
    """Return (base64_data, media_type), downscaling the long edge to _MAX_IMAGE_EDGE.

    Claude auto-resizes photos to 1568px; going further to ~768px costs ~3x fewer
    image tokens with negligible impact on calorie estimates. Re-encodes as JPEG.
    Falls back to the original bytes if the image can't be decoded."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        long_edge = max(img.size)
        if long_edge > _MAX_IMAGE_EDGE:
            scale = _MAX_IMAGE_EDGE / long_edge
            img = img.resize((round(img.width * scale), round(img.height * scale)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        logger.exception("Image downscale failed; sending the original image")
        return base64.standard_b64encode(image_bytes).decode(), media_type


def clarify(description, history, force_final=False, known_foods=None, recent=None, model=None):
    """Recompute using the Q&A so far. May ask another question unless force_final."""
    dialog = "\n".join(f"Q: {h['q']}\nA: {h['a']}" for h in history)
    force = ("\n\nDo not ask any more questions; make your best estimate now "
             "(needs_clarification false).") if force_final else ""
    return _ask(CLARIFY_PROMPT.format(description=description, dialog=dialog, force=force),
                known_foods, recent, model)


_PER_100G_PROMPT = ('Return ONLY JSON, no prose: '
                    '{"calories": 0, "protein": 0, "fat": 0, "carbs": 0}  (integers, per 100 g).')


def estimate_food(name, model=None):
    """Estimate typical macros per 100 g for a named food. Returns a dict of ints."""
    return _per_100g(f"Estimate typical macros per 100 g for: {name}.\n{_PER_100G_PROMPT}", model)


def estimate_food_from_image(image_bytes, name, media_type="image/jpeg", model=None):
    """Read macros per 100 g from a food photo / nutrition label. Returns a dict of ints."""
    text = (f"This is a photo of: {name}. If a nutrition table is visible, read the "
            f"PER-100 g column; otherwise estimate.\n{_PER_100G_PROMPT}")
    return _per_100g([_image_block(image_bytes, media_type),
                      {"type": "text", "text": text}], model)


def _per_100g(user_content, model=None):
    resp = _client_().messages.create(
        model=model or _MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": user_content}],
    )
    data = _parse(resp)
    if not all(k in data for k in ("calories", "protein", "fat", "carbs")):
        raise ValueError("estimate did not return macros")
    return data


def _foods_block(foods):
    lines = ["Known foods (per 100 g — match by name, scale by amount_g):"]
    for f in foods:
        lines.append(f"- {f['food_name']}: {int(f['calories'])} kcal, "
                     f"P{int(f['protein'])} F{int(f['fat'])} C{int(f['carbs'])}")
    return "\n".join(lines)


def _recent_block(recent):
    lines = ["Recent conversation (context for references like 'same as earlier'):"]
    for m in recent:
        who = "User" if m.get("role") == "user" else "Bot"
        lines.append(f"{who}: {m.get('text', '')}")
    return "\n".join(lines)


def _ask(user_content, known_foods=None, recent=None, model=None):
    # Stable system prompt first (cached); volatile per-user context after it.
    system = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]
    if known_foods:
        system.append({"type": "text", "text": _foods_block(known_foods)})
    if recent:
        system.append({"type": "text", "text": _recent_block(recent)})

    resp = _client_().messages.create(
        model=model or _MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse(resp)


def _parse(resp):
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    obj = _extract_json(text)
    if obj is not None:
        return obj
    # No JSON at all — the model answered in prose; treat it as a chat reply
    # rather than crashing or leaking raw text.
    return {"intent": "chat", "reply": text}


def _extract_json(text):
    """Best-effort parse of the first complete JSON object in `text`.

    The model is told to return JSON only, but it sometimes prepends reasoning
    prose (or wraps the JSON in a ```json fence). Strip a fence, then scan for the
    first brace-balanced object so that reasoning never leaks into a reply."""
    if not text:
        return None
    # Drop a leading ```json / ``` fence if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Fast path: the whole payload is JSON.
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Otherwise locate the first complete {...} object embedded in the text.
    start = text.find("{")
    while start != -1:
        candidate = _balanced_object(text, start)
        if candidate is not None:
            try:
                return json.loads(candidate)
            except ValueError:
                pass
        start = text.find("{", start + 1)
    return None


def _balanced_object(text, start):
    """Substring from `start` to its matching closing brace, or None.
    String-aware so braces inside string values don't throw off the count."""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
