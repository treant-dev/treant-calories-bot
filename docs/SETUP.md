# Prerequisites & Setup

Checklist of everything needed before implementing and deploying the calorie-tracking
Telegram bot. See [../calorie_bot_requirements.md](../calorie_bot_requirements.md) for the
full spec.

## Key decisions

| Topic            | Choice                                                                       |
|------------------|------------------------------------------------------------------------------|
| Deploy           | AWS SAM                                                                       |
| Sheets access    | User creates their own spreadsheet and adds the service account as an editor |
| Audience         | Open to everyone; minimal build, but secure and extensible                   |

> **Note — deviation from the spec.** The spec says each spreadsheet is created
> automatically. Instead, the user creates their own spreadsheet and shares it with the
> service account (editor). The bot only writes via the Sheets API — **no Drive API and no
> auto-creation needed**.

---

## 1. Telegram

- [ ] Create the bot via [@BotFather](https://t.me/BotFather) → `/newbot` → get `TELEGRAM_BOT_TOKEN`
- [ ] Generate a webhook secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"` → `TELEGRAM_WEBHOOK_SECRET`
- [ ] (Later) Register commands with BotFather: `/start`, `/tz`, `/goal`, `/calc`, `/today`, `/undo`, `/stats`, `/remember`, `/model`
- [ ] (Later) Set the webhook to the API Gateway URL, passing `secret_token=$TELEGRAM_WEBHOOK_SECRET`

## 2. Anthropic / Claude API

- [ ] Account at [console.anthropic.com](https://console.anthropic.com)
- [ ] Create `ANTHROPIC_API_KEY`
- [ ] Add a small balance (~$5 lasts a long time at ~$0.06/mo)
- [ ] Confirm access to model `claude-sonnet-4-6`

## 3. Google Cloud (Sheets API)

- [ ] Create a project in [Google Cloud Console](https://console.cloud.google.com)
- [ ] Enable **Google Sheets API** (Drive API NOT required — see note above)
- [ ] Create a **Service Account** → create a JSON key → download as `service-account.json`
- [ ] Note the `client_email` from the key — users add this address as an **Editor** on their spreadsheet
- [ ] Keep `service-account.json` out of git (already in `.gitignore`)

## 4. AWS

- [ ] AWS account
- [ ] IAM user/role with access to: Lambda, DynamoDB, API Gateway, EventBridge, CloudFormation, SSM/Secrets Manager
- [ ] Install **AWS CLI** and run `aws configure` (not installed locally yet)
- [ ] Install **AWS SAM CLI** (not installed locally yet)
- [ ] Pick a region (default in `.env.example`: `eu-central-1`)

### Secrets storage (security)

Do **not** ship secrets as plaintext Lambda env vars. Store these in **SSM Parameter Store
(SecureString)** or **Secrets Manager**, and have the function read them at cold start:

- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- the service account JSON

Non-secret config (table names, region, model id) can stay as env vars.

## 5. Local environment

- [ ] **Python 3.12** to match the Lambda runtime (local is currently 3.9 — needs upgrade)
- [ ] Create a virtualenv: `python3.12 -m venv .venv && source .venv/bin/activate`
- [ ] Install deps: `pip install -r requirements-dev.txt`
- [ ] Copy `.env.example` → `.env` and fill in values

---

## Security considerations (open to everyone)

- Validate every webhook request via the `X-Telegram-Bot-Api-Secret-Token` header.
- Onboarding gate: the bot can only write to a spreadsheet the user has explicitly shared
  with the service account, so one user cannot touch another user's sheet.
- Per-user data isolation in DynamoDB (`user_id` partition key).
- Per-user rate limit (fixed window, default 30 messages/hour) caps abuse and Claude spend
  per user — independent of the global API Gateway throttle.
- DynamoDB `user_states` rows expire via TTL (10 min) — no stale dialog state.

## Known foods — per-user memory

A personal food database so the bot remembers recurring items (e.g. "my lemon waffles =
380 kcal per 100 g") and reuses the macros instead of re-estimating or asking again.

**Storage:** new DynamoDB table `user_foods`.

```
PK: user_id (string)
SK: food_name (string, normalized lowercase)
Fields: calories, protein, fat, carbs   # all PER 100 g
        aliases (list, optional)
        updated_at
```

**How it's used during analysis:** before calling Claude, load the user's known foods and
inject them into the prompt as a reference block under `cache_control: ephemeral` (nearly
free). Claude fuzzy-matches the item name; on a match it uses the stored per-100 g macros
and just scales by `amount_g`, often skipping the clarification question. For an MVP the
whole list is sent; add filtering/retrieval only if a user's list grows large.

**How items get saved:**
- `/remember <name> <kcal> <protein> <fat> <carbs>` — explicit, values per 100 g.
- `/remember <name>` (no numbers) — opens a short dialog (state held in `user_states`):
  the user can reply with a label **photo**, the numbers, or "estimate" to have Claude guess.
- A photo with a `/remember <name>` caption — reads the nutrition label in one shot.
- Auto-offer after a clarification: once the user has supplied the missing detail, the bot
  offers to save the resolved item as a known food.

## Daily calorie budget

After every logged entry the bot also reports how many calories are left for the day:

```
Logged: omelette, 320 kcal
Today: 1450 / 2000 kcal · 550 left
```

- **Goal** is stored per user (see config fields below) and set during onboarding.
- **Consumed today** is computed by reading today's rows from the "Log" sheet and summing
  `calories`. The sheet is the source of truth, so this stays correct after manual edits
  (no drift, unlike a cached counter). One extra Sheets read per entry — cheap.
- **Scope:** calories only for now. Macro budgets (protein/fat/carbs) are a later extension —
  the schema keeps room for per-macro goals.
- **"Today" boundary** depends on the user's timezone (see below).

### Timezone

Telegram does NOT include a user's timezone in updates, so it can't be derived from a plain
text message. Onboarding offers a choice:

- **Share location** — one-tap "send location" button → coordinates → IANA timezone via a
  keyless tz API (single `httpx` call, e.g. timeapi.io). Keeps the Lambda package light
  (no large offline dataset).
- **Manual** — `/tz Europe/Stockholm`.

`/tz` also works later as an override (e.g. after moving).

## Per-user record (DynamoDB `user_profile`)

```
PK: user_id (string)
Fields: spreadsheet_id
        timezone              # IANA name, e.g. "Europe/Stockholm"
        daily_calorie_goal    # integer kcal
        profile (optional)    # {sex, weight_kg, height_cm, age, activity} — kept if goal was computed, for recompute
        model (optional)      # chosen Claude model id (set via /model); falls back to ANTHROPIC_MODEL
        rl_window, rl_count            # per-user rate-limit window (fixed window)
        recent                          # last N messages (conversation buffer)
# No per-day state here: meal_no and the daily total are both derived from the sheet
# (the source of truth). meal_no continues a meal if logged within 37 min of the
# previous entry, else starts the next number.
```

## Commands

`/start` · `/tz` · `/goal` · `/calc` · `/today` · `/undo` · `/stats` · `/remember` · `/model`

(`/calc <food>` — alias `/estimate`, or a photo with a `/calc` caption — estimates
calories without logging anything. `/undo` removes the last logged meal. `/model` shows
inline buttons to switch the analysis model (Sonnet 4.6 default, Haiku 4.5 cheapest, Opus
4.8 most accurate); the choice is stored per user and overrides the `ANTHROPIC_MODEL`
default. Intent is also inferred from phrasing, so plain language usually works without a
command. Bot replies in the user's own language automatically — no language setting needed.)

## Onboarding flow (revised)

1. User sends `/start`.
2. **Spreadsheet:** bot replies with the service account email and instructions — create a
   Google Sheet, share it as Editor with that email, then send the spreadsheet link/ID.
   Bot validates write access, stores `spreadsheet_id`, and initializes the "Log" headers.
3. **Timezone:** bot offers a choice — share location (→ tz API) or set it manually via `/tz`.
4. **Calorie goal:** bot offers a choice —
   - set it directly: `/goal 2000`, or
   - compute it: bot asks sex / weight / height / age / activity level and estimates the
     daily target via Mifflin–St Jeor (BMR × activity factor). Profile is stored so the goal
     can be recomputed later.
5. From then on, photos/text are analyzed, appended to the sheet, and the bot replies with
   the entry total plus calories remaining for the day.

Bot replies always mirror the user's language automatically (Claude follows the input
language), so there is no `/lang` command or stored language preference.
