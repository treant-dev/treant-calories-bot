"""DynamoDB access: per-user record (user_profile), dialog state (user_states),
and known foods (user_foods)."""
import os
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

_dynamo = None
_STATE_TTL_SECONDS = 3600  # pending clarification lives 1 hour
_RATE_LIMIT = 30           # max messages per user per window
_RATE_WINDOW = 3600        # rate-limit window, seconds


def _is_conditional_failure(err):
    return err.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _table(env_var):
    global _dynamo
    if _dynamo is None:
        _dynamo = boto3.resource("dynamodb")
    return _dynamo.Table(os.environ[env_var])


# ── user profile (user_profile) ───────────────────────────────
def get_user(user_id):
    resp = _table("USER_PROFILE_TABLE").get_item(Key={"user_id": str(user_id)})
    return resp.get("Item")


def set_spreadsheet(user_id, spreadsheet_id):
    _table("USER_PROFILE_TABLE").update_item(
        Key={"user_id": str(user_id)},
        UpdateExpression="SET spreadsheet_id = :s",
        ExpressionAttributeValues={":s": spreadsheet_id},
    )


def set_goal(user_id, goal):
    _table("USER_PROFILE_TABLE").update_item(
        Key={"user_id": str(user_id)},
        UpdateExpression="SET daily_calorie_goal = :g",
        ExpressionAttributeValues={":g": int(goal)},
    )


def set_timezone(user_id, tz):
    # "timezone" is a DynamoDB reserved word, so alias it.
    _table("USER_PROFILE_TABLE").update_item(
        Key={"user_id": str(user_id)},
        UpdateExpression="SET #tz = :t",
        ExpressionAttributeNames={"#tz": "timezone"},
        ExpressionAttributeValues={":t": tz},
    )


# ── conversation buffer (recent messages, capped) ─────────────
_RECENT_CAP = 6


def get_recent(user_id):
    user = get_user(user_id)
    return user.get("recent", []) if user else []


def push_recent(user_id, role, text):
    recent = (get_recent(user_id) + [{"role": role, "text": str(text)[:400]}])[-_RECENT_CAP:]
    _table("USER_PROFILE_TABLE").update_item(
        Key={"user_id": str(user_id)},
        UpdateExpression="SET recent = :r",
        ExpressionAttributeValues={":r": recent},
    )


# ── dialog state (user_states) ────────────────────────────────
def get_pending(user_id):
    resp = _table("USER_STATES_TABLE").get_item(Key={"user_id": str(user_id)})
    item = resp.get("Item")
    if not item:
        return None
    # TTL deletion is not instant; ignore expired rows that linger.
    if int(item.get("ttl", 0)) < int(time.time()):
        return None
    return item.get("pending_entry")


def set_pending(user_id, pending_entry):
    _table("USER_STATES_TABLE").put_item(Item={
        "user_id": str(user_id),
        "pending_entry": pending_entry,
        "ttl": int(time.time()) + _STATE_TTL_SECONDS,
    })


def clear_pending(user_id):
    _table("USER_STATES_TABLE").delete_item(Key={"user_id": str(user_id)})


# ── known foods (user_foods) ──────────────────────────────────
def put_food(user_id, name, calories, protein, fat, carbs):
    """Save/overwrite a known food, macros per 100 g."""
    _table("USER_FOODS_TABLE").put_item(Item={
        "user_id": str(user_id),
        "food_name": name.strip().lower(),
        "calories": int(calories),
        "protein": int(protein),
        "fat": int(fat),
        "carbs": int(carbs),
    })


def list_foods(user_id):
    resp = _table("USER_FOODS_TABLE").query(
        KeyConditionExpression=Key("user_id").eq(str(user_id)))
    return resp.get("Items", [])


# ── per-day meal counter (no sheet read, race-safe) ──
def next_meal(user_id, date):
    """Allocate this date's next meal_no. Returns meal_no. Atomic +1, resetting
    when the day rolls over — the same 'last + 1 or reset' logic, kept in DynamoDB
    instead of the sheet. The daily calorie total is NOT cached here; it is summed
    from the sheet (the source of truth, so manual edits are always reflected)."""
    table = _table("USER_PROFILE_TABLE")
    try:
        resp = table.update_item(
            Key={"user_id": str(user_id)},
            UpdateExpression="ADD meal_no :one",
            ConditionExpression="meal_date = :d",        # same day → just increment
            ExpressionAttributeValues={":one": 1, ":d": date},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        if not _is_conditional_failure(e):
            raise
        resp = table.update_item(                         # new day / first meal → reset
            Key={"user_id": str(user_id)},
            UpdateExpression="SET meal_date = :d, meal_no = :one",
            ExpressionAttributeValues={":d": date, ":one": 1},
            ReturnValues="UPDATED_NEW",
        )
    return int(resp["Attributes"]["meal_no"])


def set_day_start_row(user_id, row):
    """Remember the sheet row where today's first meal landed (for bounded reads)."""
    _table("USER_PROFILE_TABLE").update_item(
        Key={"user_id": str(user_id)},
        UpdateExpression="SET day_start_row = :r",
        ExpressionAttributeValues={":r": int(row)},
    )


# ── per-user rate limit (fixed window) ────────────────────────
def allow_request(user_id):
    """True if the user is under the message cap for the current window."""
    now = int(time.time())
    table = _table("USER_PROFILE_TABLE")
    try:
        resp = table.update_item(
            Key={"user_id": str(user_id)},
            UpdateExpression="ADD rl_count :one",
            ConditionExpression="attribute_exists(rl_window) AND rl_window > :cutoff",
            ExpressionAttributeValues={":one": 1, ":cutoff": now - _RATE_WINDOW},
            ReturnValues="UPDATED_NEW",
        )
        count = int(resp["Attributes"]["rl_count"])
    except ClientError as e:
        if not _is_conditional_failure(e):
            raise
        table.update_item(                                # window expired / first ever → reset
            Key={"user_id": str(user_id)},
            UpdateExpression="SET rl_window = :now, rl_count = :one",
            ExpressionAttributeValues={":now": now, ":one": 1},
        )
        count = 1
    return count <= _RATE_LIMIT
