"""AWS Lambda entry point for the Telegram webhook.

Flow: onboard the user (link a Google Sheet), then for each meal (text or photo)
run Claude analysis. If a detail is ambiguous the bot asks clarifying questions —
up to a few rounds, state held in DynamoDB — then either logs the result and
replies with the daily total (default), or just replies with an estimate (/calc).
"""
import json
import logging
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

import claude
import dynamo
import geo
import sheets
import telegram
from secret_store import get_secret_optional

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_MAX_CLARIFY_ROUNDS = 3  # cap the back-and-forth so it always converges

# Models the user can switch between via /model. Sonnet is the default (see
# ANTHROPIC_MODEL); the value stored per user overrides it.
_MODELS = {
    "sonnet": ("claude-sonnet-4-6", "Sonnet 4.6 — balanced (default)"),
    "haiku": ("claude-haiku-4-5", "Haiku 4.5 — fastest & cheapest"),
    "opus": ("claude-opus-4-8", "Opus 4.8 — most accurate, priciest"),
}


def lambda_handler(event, context):
    # Only Telegram should be able to invoke us: it echoes back the secret we
    # registered via setWebhook in the X-Telegram-Bot-Api-Secret-Token header.
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    expected = get_secret_optional("telegram_webhook_secret")
    if expected and headers.get("x-telegram-bot-api-secret-token") != expected:
        logger.warning("Rejected webhook call with a bad secret token")
        return {"statusCode": 403, "body": "forbidden"}

    update = json.loads(event.get("body") or "{}")
    logger.info("Received Telegram update: %s", update.get("update_id"))

    callback = update.get("callback_query")
    if callback:
        try:
            _handle_callback(callback)
        except Exception:
            logger.exception("Callback error")
        return {"statusCode": 200, "body": "ok"}

    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (message.get("from") or {}).get("id")
    text = message.get("text")

    if chat_id and user_id and (text or message.get("photo") or message.get("location")):
        if not dynamo.allow_request(user_id):
            telegram.send_message(chat_id, "Too many requests — please wait a bit and try again.")
            return {"statusCode": 200, "body": "ok"}
        try:
            telegram.send_chat_action(chat_id)  # show "typing…" while we work
            if text:
                _route_text(chat_id, user_id, text.strip())
            elif message.get("photo"):
                _route_photo(chat_id, user_id, message)
            else:
                _handle_location(chat_id, user_id, message["location"])
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code in (403, 404):
                logger.warning("Sheet unreachable: %s", e.response.status_code)
                telegram.send_message(
                    chat_id,
                    "I can't reach your Google Sheet — make sure it's still shared with my "
                    "service-account email (send /start to see it), or send /start to relink.")
            else:
                logger.exception("Handler error")
                telegram.send_message(chat_id, "Something went wrong — please try again.")
        except Exception:
            logger.exception("Handler error")
            telegram.send_message(chat_id, "Something went wrong — please try again.")

    # Telegram needs a fast 200 or it retries the delivery.
    return {"statusCode": 200, "body": "ok"}


# ── routing ───────────────────────────────────────────────────
def _route_text(chat_id, user_id, text):
    if text.startswith("/start"):
        return _onboard_start(chat_id)
    if text.startswith("/goal"):
        return _set_goal(chat_id, user_id, text)
    if text.startswith("/tz"):
        return _set_timezone(chat_id, user_id, text)
    if text.startswith(("/calc", "/estimate")):
        return _new_meal(chat_id, user_id, _strip_command(text), forced_mode="estimate")
    if text.startswith("/remember"):
        return _remember(chat_id, user_id, text)
    if text.startswith("/today"):
        return _today(chat_id, user_id)
    if text.startswith("/undo"):
        return _undo(chat_id, user_id)
    if text.startswith("/model"):
        return _choose_model(chat_id, user_id)

    link = _SHEET_URL_RE.search(text)
    if link:
        return _link_sheet(chat_id, user_id, link.group(1))

    # A reply during an open clarification continues that dialog.
    pending = dynamo.get_pending(user_id)
    if pending:
        return _continue_clarify(chat_id, user_id, pending, text)

    _new_meal(chat_id, user_id, text)


def _route_photo(chat_id, user_id, message):
    caption = (message.get("caption") or "").strip()
    file_id = message["photo"][-1]["file_id"]  # last entry = highest resolution
    image = telegram.download_file(telegram.get_file_path(file_id))

    # A photo of a label/package with a /remember caption saves a known food.
    if caption.startswith("/remember"):
        return _remember_photo(chat_id, user_id, _strip_command(caption), image)

    forced_estimate = caption.startswith(("/calc", "/estimate"))
    if forced_estimate:
        caption = _strip_command(caption)
    result = claude.analyze_image(image, caption=caption or None,
                                  known_foods=dynamo.list_foods(user_id),
                                  recent=dynamo.get_recent(user_id),
                                  model=dynamo.get_model(user_id))
    dynamo.push_recent(user_id, "user", caption or "(photo)")
    mode = "estimate" if forced_estimate else _intent_mode(result)
    _process(chat_id, user_id, result, description=caption or "(meal from photo)", mode=mode)


# ── meal analysis + clarification loop ────────────────────────
def _new_meal(chat_id, user_id, description, forced_mode=None):
    if not description:
        return telegram.send_message(chat_id, "Usage: /calc two eggs and toast")
    result = claude.analyze_text(description, known_foods=dynamo.list_foods(user_id),
                                 recent=dynamo.get_recent(user_id),
                                 model=dynamo.get_model(user_id))
    dynamo.push_recent(user_id, "user", description)
    mode = forced_mode or _intent_mode(result)
    _process(chat_id, user_id, result, description=description, mode=mode)


def _intent_mode(result):
    # Claude classifies phrasing; default to logging.
    intent = result.get("intent")
    return intent if intent in ("estimate", "chat", "undo", "correct") else "log"


def _process(chat_id, user_id, result, description, mode):
    """Reply conversationally (chat), undo, ask a clarifying question, or finalize."""
    if mode == "chat":
        return _reply(chat_id, user_id, result.get("reply") or "Got it.")
    if mode == "undo":
        return _undo(chat_id, user_id)
    if result.get("needs_clarification"):
        dynamo.set_pending(user_id, {
            "mode": mode,
            "description": description,
            "question": result.get("question", ""),
            "history": [],
        })
        return _reply(chat_id, user_id, result.get("question", "Could you clarify?"))
    _finalize(chat_id, user_id, result, mode)


def _continue_clarify(chat_id, user_id, pending, answer):
    history = pending.get("history", []) + [
        {"q": pending.get("question", ""), "a": answer},
    ]
    force = len(history) >= _MAX_CLARIFY_ROUNDS
    result = claude.clarify(pending.get("description", ""), history, force_final=force,
                            known_foods=dynamo.list_foods(user_id),
                            recent=dynamo.get_recent(user_id),
                            model=dynamo.get_model(user_id))
    dynamo.push_recent(user_id, "user", answer)

    if result.get("needs_clarification") and not force:
        dynamo.set_pending(user_id, {
            "mode": pending.get("mode", "log"),
            "description": pending.get("description", ""),
            "question": result.get("question", ""),
            "history": history,
        })
        return _reply(chat_id, user_id, result.get("question", "Could you clarify?"))

    dynamo.clear_pending(user_id)
    _finalize(chat_id, user_id, result, pending.get("mode", "log"))


def _finalize(chat_id, user_id, result, mode):
    if mode == "estimate":
        return _reply(chat_id, user_id, _format_estimate(result))

    user = _require_onboarded(chat_id, user_id)
    if not user:
        return
    items = result.get("items", [])
    if not items:
        return _reply(chat_id, user_id, "Couldn't identify any food there.")

    sid = user["spreadsheet_id"]
    tz = user.get("timezone", "UTC")
    date, time_str = sheets.now_parts(tz)

    # A correction replaces the last entry: drop it, then log the fix.
    if mode == "correct":
        sheets.delete_last_meal(sid)

    # meal_no and the prior day total both come from the sheet (the source of truth):
    # the number continues the current meal if logged within the hour, else starts a
    # new one. No per-day counter in DynamoDB to drift.
    meal_no, prior_total, _ = sheets.day_state(sid, date, tz)
    sheets.append_meal(sid, items, meal_no, date, time_str)
    day_total = prior_total + sum(_as_int(it.get("calories")) for it in items)
    label = "Updated" if mode == "correct" else "Logged"
    _reply(chat_id, user_id, _format_logged(result, day_total, user, label))


def _undo(chat_id, user_id):
    user = _require_onboarded(chat_id, user_id)
    if not user:
        return
    removed = sheets.delete_last_meal(user["spreadsheet_id"])
    if not removed:
        return _reply(chat_id, user_id, "Nothing to undo.")
    names = ", ".join(name for name, _ in removed)
    _reply(chat_id, user_id, f"Removed: {names}.")


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _reply(chat_id, user_id, text):
    """Send a reply and record it in the conversation buffer."""
    telegram.send_message(chat_id, text)
    try:
        dynamo.push_recent(user_id, "assistant", text)
    except Exception:
        logger.exception("Failed to record reply in conversation buffer")


# ── commands / onboarding ─────────────────────────────────────
def _onboard_start(chat_id):
    email = sheets.service_account_email()
    telegram.send_message(
        chat_id,
        "Welcome! To track your meals:\n"
        "1. Create a Google Sheet.\n"
        f"2. Share it as Editor with:\n{email}\n"
        "3. Send me the link to that sheet.\n\n"
        "Then describe a meal or send a photo and I'll log it.\n"
        "Optional: set a daily target with /goal 2000.\n"
        "Use /calc <food> (or a photo with a /calc caption) to estimate without logging.",
    )


def _link_sheet(chat_id, user_id, spreadsheet_id):
    try:
        sheets.validate_access(spreadsheet_id)
        sheets.ensure_log_sheet(spreadsheet_id)
    except Exception:
        logger.exception("Sheet link failed")
        return telegram.send_message(
            chat_id,
            "I can't access that sheet. Share it as Editor with my service-account "
            "email (send /start to see it), then send the link again.")
    dynamo.set_spreadsheet(user_id, spreadsheet_id)
    telegram.send_message(chat_id, "Sheet linked. Describe a meal and I'll log it.")


def _set_goal(chat_id, user_id, text):
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return telegram.send_message(chat_id, "Usage: /goal 2000")
    goal = int(parts[1])
    dynamo.set_goal(user_id, goal)
    telegram.send_message(chat_id, f"Daily goal set to {goal} kcal.")


def _set_timezone(chat_id, user_id, text):
    arg = _strip_command(text)
    if arg:
        try:
            ZoneInfo(arg)  # validate it's a real IANA name
        except (ZoneInfoNotFoundError, ValueError):
            return telegram.send_message(
                chat_id, "Unknown timezone. Try e.g. /tz Europe/Stockholm, or share your location.")
        dynamo.set_timezone(user_id, arg)
        return telegram.send_message(chat_id, f"Timezone set to {arg}.",
                                     reply_markup=telegram.remove_keyboard())
    telegram.send_message(
        chat_id,
        "Share your location and I'll set your timezone — or type it, e.g. /tz Europe/Stockholm.",
        reply_markup=telegram.location_keyboard())


def _handle_location(chat_id, user_id, location):
    try:
        tz = geo.timezone_for(location["latitude"], location["longitude"])
    except Exception:
        logger.exception("Timezone lookup failed")
        return telegram.send_message(
            chat_id, "Couldn't determine your timezone. Set it manually: /tz Europe/Stockholm")
    dynamo.set_timezone(user_id, tz)
    telegram.send_message(chat_id, f"Timezone set to {tz}.",
                          reply_markup=telegram.remove_keyboard())


def _remember(chat_id, user_id, text):
    parts = text.split()[1:]  # drop the /remember token
    if not parts:
        return telegram.send_message(
            chat_id, "Usage: /remember <name> [kcal protein fat carbs]  (per 100 g; "
                     "omit the numbers and I'll estimate them)")

    if len(parts) >= 5 and all(p.lstrip("-").isdigit() for p in parts[-4:]):
        *name, cal, prot, fat, carb = parts
        return _save_food(chat_id, user_id, " ".join(name), cal, prot, fat, carb)

    name = " ".join(parts)
    try:
        est = claude.estimate_food(name, model=dynamo.get_model(user_id))
    except Exception:
        logger.exception("estimate_food failed")
        return telegram.send_message(
            chat_id, "Couldn't estimate that. Try: /remember <name> <kcal> <protein> <fat> <carbs>")
    _save_food(chat_id, user_id, name, est["calories"], est["protein"], est["fat"], est["carbs"])


def _remember_photo(chat_id, user_id, name, image):
    if not name:
        return telegram.send_message(
            chat_id, "Send the photo with a caption like: /remember lemon waffles")
    try:
        est = claude.estimate_food_from_image(image, name, model=dynamo.get_model(user_id))
    except Exception:
        logger.exception("estimate_food_from_image failed")
        return telegram.send_message(
            chat_id, "Couldn't read that label. Try: /remember <name> <kcal> <protein> <fat> <carbs>")
    _save_food(chat_id, user_id, name, est["calories"], est["protein"], est["fat"], est["carbs"])


def _save_food(chat_id, user_id, name, cal, prot, fat, carb):
    dynamo.put_food(user_id, name, cal, prot, fat, carb)
    telegram.send_message(
        chat_id,
        f"Saved {name}: {int(cal)} kcal per 100 g (P{int(prot)} F{int(fat)} C{int(carb)}).")


def _today(chat_id, user_id):
    user = _require_onboarded(chat_id, user_id)
    if not user:
        return
    date, _ = sheets.now_parts(user.get("timezone", "UTC"))
    total, items = sheets.day_summary(user["spreadsheet_id"], date)
    if not items:
        return telegram.send_message(chat_id, "No meals logged today yet.")
    lines = [f"{name} — {cal} kcal" for name, cal in items]
    goal = user.get("daily_calorie_goal")
    if goal:
        lines.append(f"\nToday: {total} / {int(goal)} kcal · {int(goal) - total} left")
    else:
        lines.append(f"\nToday: {total} kcal")
    telegram.send_message(chat_id, "\n".join(lines))


# ── model selection ───────────────────────────────────────────
def _choose_model(chat_id, user_id):
    current = dynamo.get_model(user_id) or claude.default_model()
    rows = [[(("• " if mid == current else "") + label, f"model:{key}")]
            for key, (mid, label) in _MODELS.items()]
    telegram.send_message(
        chat_id,
        "Choose the model used to analyze your meals (affects accuracy and cost):",
        reply_markup=telegram.inline_keyboard(rows))


def _handle_callback(callback):
    data = callback.get("data") or ""
    cq_id = callback.get("id")
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    user_id = (callback.get("from") or {}).get("id")
    choice = _model_choice(data)
    if choice and chat_id and user_id:
        dynamo.set_model(user_id, choice[0])
        telegram.answer_callback_query(cq_id, "Model updated")
        return telegram.send_message(chat_id, f"Model set to {choice[1]}.")
    telegram.answer_callback_query(cq_id)


def _model_choice(data):
    """Map an inline-button 'model:<key>' payload to (model_id, label), or None."""
    if not data.startswith("model:"):
        return None
    return _MODELS.get(data.split(":", 1)[1])


def _require_onboarded(chat_id, user_id):
    user = dynamo.get_user(user_id)
    if not user or not user.get("spreadsheet_id"):
        telegram.send_message(
            chat_id, "You haven't linked a Google Sheet yet. Send /start to set up.")
        return None
    return user


# ── helpers ───────────────────────────────────────────────────
def _strip_command(text):
    """Drop a leading /command token, returning the rest."""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _item_lines(result):
    return [
        f"{it['name']} — {it['calories']} kcal "
        f"(P{it['protein']} F{it['fat']} C{it['carbs']})"
        for it in result.get("items", [])
    ]


def _total_line(result, label):
    t = result.get("total", {})
    return (f"\n{label}: {t.get('calories', 0)} kcal · "
            f"P{t.get('protein', 0)} F{t.get('fat', 0)} C{t.get('carbs', 0)}")


def _format_logged(result, day_total, user, label="Logged"):
    lines = _item_lines(result)
    lines.append(_total_line(result, label))
    goal = user.get("daily_calorie_goal")
    if goal:
        lines.append(f"Today: {day_total} / {int(goal)} kcal · {int(goal) - day_total} left")
    else:
        lines.append(f"Today: {day_total} kcal")
    return "\n".join(lines)


def _format_estimate(result):
    if not result.get("items"):
        return "Couldn't estimate that."
    return "\n".join(["Estimate (not logged):", *_item_lines(result),
                      _total_line(result, "Total")])
