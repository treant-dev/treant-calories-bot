# Calorie Tracking Telegram Bot — Requirements

## Concept

A Telegram bot for calorie tracking. The user sends a photo of their food or a text
description, the bot analyzes it via the Claude API, asks a clarifying question when needed,
and writes the result into a personal Google Sheet.

---

## Architecture

```
Telegram → API Gateway → Lambda
                            ├── DynamoDB (user_profile + user_states + user_foods)
                            ├── Claude API
                            └── Google Sheets API (final writes)
```

### Components

- **Telegram Bot** — webhook (not polling)
- **AWS API Gateway** — HTTP endpoint for the webhook
- **AWS Lambda** — core logic, Python
- **AWS DynamoDB** — two tables: user mapping and dialog state
- **Claude API** — photo/text analysis (model: claude-sonnet-4-6)
- **Google Sheets API** — a personal sheet per user

---

## DynamoDB — tables

### user_profile
The per-user record: linked sheet + config, plus per-day meal counters, the rate-limit
window, and the conversation buffer.

```
PK: user_id (string)
Fields: spreadsheet_id, timezone, daily_calorie_goal,
        meal_date, meal_no, day_kcal, day_start_row,   # per-day counters
        rl_window, rl_count,                            # rate limit
        recent                                          # conversation buffer
```

### user_states
Dialog state when the bot has asked a clarifying question. TTL 1 hour.

```
PK: user_id (string)
Fields: state, pending_entry (JSON), ttl
```

- `ttl` — Unix epoch **in seconds**, set by the app on write as `now + 3600` (1 hour).
  DynamoDB has no "duration" setting; the table only knows the attribute name (`ttl`), and
  deletion timing is driven entirely by this value.

---

## Google Sheets — structure

Each user gets a separate Google Sheet, created automatically on first contact.

### "Log" sheet

| meal_no | date | time | item | amount_g | calories | protein | fat | carbs |
|---------|------|------|------|----------|----------|---------|-----|-------|

- `meal_no` — the sequential number of the meal **within a date** (integer), reset every day.
  A meal's uniqueness = the `(date, meal_no)` pair.
- Allocated by an **atomic DynamoDB counter** on the user record (increment, reset on a new
  day) — not derived from the sheet. So logging needs no full-sheet read and is race-safe.
  The running daily total is kept the same way; the sheet stays the source of truth for
  `/today` and `/stats`.
- A single meal may span several rows (one row = one dish/ingredient); all rows of the same
  meal share the same `meal_no`.

### "Days" sheet (optional)
Per-day aggregation via Google Sheets formulas — total calories and macros.

---

## Dialog flow

### Without clarification
```
User: [photo/text]
Claude: confident in the estimate
→ save to Sheets
→ reply with the total
```

### With clarification
```
User: [photo/text]
Claude: not confident
→ save state to DynamoDB
→ ask 1 clarifying question

User: answer
Claude: recompute with the clarification
→ save to Sheets
→ delete state
→ reply with the total
```

### "Log it roughly" (skip clarification)
An explicit user command to estimate as-is, without clarifying questions.

- Markers: "log it roughly", "as is", "don't ask", etc.
- Claude forcibly sets `needs_clarification: false`, even when the portion/composition is
  unclear, and uses typical assumptions.
- Works both on the first message and in reply to a clarifying question (on the second pass
  the question is NOT asked again).
- Marking: every dish of such a meal is recorded with a `~ ` prefix in the `item` field (the
  whole meal is marked).
- The bot's reply should honestly note that the estimate is approximate and offer to refine
  it for a recompute.

---

## Claude API — prompts

### Initial analysis

```
Analyze the food in the photo / description.
Return ONLY JSON:
{
  "needs_clarification": true/false,
  "question": "...",      // if needs_clarification = true (1 question!)
  "items": [              // if needs_clarification = false
    {
      "name": "...",
      "amount_g": 0,
      "calories": 0,
      "protein": 0,
      "fat": 0,
      "carbs": 0
    }
  ],
  "total": { "calories": 0, "protein": 0, "fat": 0, "carbs": 0 }
}

needs_clarification = true if:
- the portion size is not visible and it strongly affects calories
- it's unclear what the dish contains (porridge on water or milk)
- a sauce/dressing isn't visible but is significant

needs_clarification = false if:
- the portion is obvious (a whole banana, 2 eggs)
- a standard package
- the user already specified grams
```

### Clarification

```
Dish: {pending_entry}
Clarification from the user: {user_message}
Recompute the calories taking the clarification into account.
Return the final JSON in the same format (without needs_clarification).
```

---

## Lambda optimization

- System prompt with `cache_control: ephemeral` for Prompt Caching
- Global initialization of clients outside `lambda_handler`
- Lightweight HTTP calls to the Google Sheets API directly (no `google-api-python-client`)
- Ping via EventBridge every 5 minutes to keep it warm
- Lambda timeout: 30 seconds
- Return `200 OK` to Telegram immediately, do the processing before replying

### Photos
- Claude auto-resizes to 1568px
- Optional: resize to 768px before sending to save tokens

---

## Cost (estimate)

| Component | Cost |
|---|---|
| Lambda | free (free tier) |
| DynamoDB | free (free tier) |
| API Gateway | free (free tier) |
| Claude API (~1000 photos/mo) | ~$0.06 |
| Google Sheets API | free |
| **Total** | **~$0.06/mo** |

---

## Stack

- Python 3.12
- `python-telegram-bot` or direct calls to the Telegram API
- `httpx` for the Google Sheets API (instead of the heavy SDK)
- `anthropic` SDK
- `boto3` for DynamoDB

---

## TODO / open questions

- [ ] `/stats` command — totals per day/week/month (read from Sheets or compute on the fly)
- [ ] Multi-user support — onboarding needed (sheet creation)
- [ ] Sheets API error handling (retry logic)
- [ ] Deploy — SAM / Serverless Framework / manual zip
