"""Minimal Telegram Bot API client over httpx (no heavy SDK)."""
import httpx

from secret_store import get_secret

_API = "https://api.telegram.org"


def send_message(chat_id, text, reply_markup=None):
    """Send a plain-text message to a chat (with an optional keyboard)."""
    token = get_secret("telegram_bot_token")
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = httpx.post(f"{_API}/bot{token}/sendMessage", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def location_keyboard():
    """A one-tap button that asks the user to share their location."""
    return {
        "keyboard": [[{"text": "Share location", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def remove_keyboard():
    return {"remove_keyboard": True}


def inline_keyboard(rows):
    """Build an inline keyboard from rows of (text, callback_data) pairs."""
    return {"inline_keyboard": [
        [{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def answer_callback_query(callback_query_id, text=None):
    """Acknowledge a tapped inline button so the client stops its loading spinner."""
    token = get_secret("telegram_bot_token")
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        httpx.post(f"{_API}/bot{token}/answerCallbackQuery", json=payload, timeout=10)
    except Exception:
        pass  # cosmetic — never fail the request over this


def send_chat_action(chat_id, action="typing"):
    """Show a status like 'typing…' (best-effort; lasts ~5s or until a reply)."""
    token = get_secret("telegram_bot_token")
    try:
        httpx.post(f"{_API}/bot{token}/sendChatAction",
                   json={"chat_id": chat_id, "action": action}, timeout=10)
    except Exception:
        pass  # cosmetic — never fail the request over this


def get_file_path(file_id):
    """Resolve a Telegram file_id to its downloadable file path."""
    token = get_secret("telegram_bot_token")
    resp = httpx.get(f"{_API}/bot{token}/getFile",
                     params={"file_id": file_id}, timeout=10)
    resp.raise_for_status()
    return resp.json()["result"]["file_path"]


def download_file(file_path):
    """Download a file's bytes given its path from get_file_path."""
    token = get_secret("telegram_bot_token")
    resp = httpx.get(f"{_API}/file/bot{token}/{file_path}", timeout=20)
    resp.raise_for_status()
    return resp.content
