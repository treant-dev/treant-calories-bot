"""Food analysis via the Claude API."""
import base64
import json
import os

from anthropic import Anthropic

from prompts import CLARIFY_PROMPT, SYSTEM_PROMPT
from secret_store import get_secret

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
_client = None


def _client_():
    # Lazily built and reused across warm invocations.
    global _client
    if _client is None:
        _client = Anthropic(api_key=get_secret("anthropic_api_key"))
    return _client


def analyze_text(description, known_foods=None, recent=None):
    """Analyze a text meal description. Returns the parsed JSON dict."""
    return _ask(description, known_foods, recent)


def analyze_image(image_bytes, media_type="image/jpeg", caption=None,
                  known_foods=None, recent=None):
    """Analyze a food photo (optionally with the user's caption)."""
    content = [{
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(image_bytes).decode(),
        },
    }, {
        "type": "text",
        "text": caption or "Analyze the food in this photo.",
    }]
    return _ask(content, known_foods, recent)


def clarify(description, history, force_final=False, known_foods=None, recent=None):
    """Recompute using the Q&A so far. May ask another question unless force_final."""
    dialog = "\n".join(f"Q: {h['q']}\nA: {h['a']}" for h in history)
    force = ("\n\nDo not ask any more questions; make your best estimate now "
             "(needs_clarification false).") if force_final else ""
    return _ask(CLARIFY_PROMPT.format(description=description, dialog=dialog, force=force),
                known_foods, recent)


_PER_100G_PROMPT = ('Return ONLY JSON, no prose: '
                    '{"calories": 0, "protein": 0, "fat": 0, "carbs": 0}  (integers, per 100 g).')


def estimate_food(name):
    """Estimate typical macros per 100 g for a named food. Returns a dict of ints."""
    return _per_100g(f"Estimate typical macros per 100 g for: {name}.\n{_PER_100G_PROMPT}")


def estimate_food_from_image(image_bytes, name, media_type="image/jpeg"):
    """Read macros per 100 g from a food photo / nutrition label. Returns a dict of ints."""
    text = (f"This is a photo of: {name}. If a nutrition table is visible, read the "
            f"PER-100 g column; otherwise estimate.\n{_PER_100G_PROMPT}")
    return _per_100g([{
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(image_bytes).decode(),
        },
    }, {"type": "text", "text": text}])


def _per_100g(user_content):
    resp = _client_().messages.create(
        model=_MODEL,
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


def _ask(user_content, known_foods=None, recent=None):
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
        model=_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse(resp)


def _parse(resp):
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Tolerate a ```json ... ``` fence if the model adds one.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except ValueError:
        # Model answered in prose instead of JSON — treat it as a chat reply
        # rather than crashing.
        return {"intent": "chat", "reply": text}
