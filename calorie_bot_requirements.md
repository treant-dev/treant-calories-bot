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
        model,                                          # chosen Claude model (set via /model)
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
- **Derived from the sheet** at log time, not from a DynamoDB counter: a new entry continues
  the current meal (same `meal_no`) if it is logged within **37 minutes** of the previous
  entry, otherwise it starts the next number. Because it is computed from the sheet, the
  numbering starts at 1, reuses a number freed by undoing/correcting the last entry, and is
  never burned by a failed append.
- The **daily calorie total is not cached** either — it is summed from the sheet on every log
  reply and on `/today`. The sheet is the single source of truth, so manual edits/writes are
  always reflected and nothing can drift. There is no per-day state in DynamoDB at all.
- A single meal may span several rows and several appends; all rows logged within the window
  share the same `meal_no`.
- **Undo/correct** operate on the *last entry* (the rows of the most recent append, grouped by
  a shared `date`+`time` at minute precision), not the whole meal — so undoing removes only the
  last thing logged even when several entries share a `meal_no`.

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
- Claude auto-resizes to 1568px, but the app **downscales the long edge to 768px**
  (Pillow, re-encoded as JPEG) before sending — ~3x fewer image tokens with negligible
  impact on calorie estimates. Falls back to the original bytes if an image can't be decoded.

---

## Cost (estimate)

Cost is dominated almost entirely by the Claude API; the AWS pieces and Sheets are
effectively free at this scale. With photos downscaled to 768px, a Sonnet 4.6 photo
analysis is roughly **$0.008** (≈1600 input + ~200 output tokens) and a text analysis
~$0.006.

| Component | Cost |
|---|---|
| Lambda, DynamoDB, API Gateway | free tier (negligible) |
| Google Sheets API | free |
| Claude API (Sonnet 4.6) | the only real cost — see per-call figures above |

Per **active user**, that works out to roughly **$10–25/year** (light use ~$8–10,
heavy use ~$40+). Switching the model to Haiku 4.5 cuts the Claude cost ~3x.

---

## Stack

- Python 3.12
- `python-telegram-bot` or direct calls to the Telegram API
- `httpx` for the Google Sheets API (instead of the heavy SDK)
- `anthropic` SDK
- `Pillow` to downscale photos before sending to Claude
- `boto3` for DynamoDB

---

## TODO / open questions

- [ ] `/stats` command — totals per day/week/month (read from Sheets or compute on the fly)
- [ ] Multi-user support — onboarding needed (sheet creation)
- [ ] Sheets API error handling (retry logic)
- [ ] Deploy — SAM / Serverless Framework / manual zip
