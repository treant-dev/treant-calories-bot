# Calorie-Tracking Telegram Bot

A Telegram bot for calorie tracking. Send a photo of your food or a text
description; the bot analyzes it with the Claude API, asks a clarifying question
when needed, and appends the result to your personal Google Sheet. It also
reports how many calories you have left for the day.

Runs serverless on AWS (Lambda + API Gateway + DynamoDB) and costs roughly
**$0.06/month** at ~1000 photos.

## Architecture

```
Telegram → API Gateway (throttled) → Lambda
                                        ├── DynamoDB  (user_profile, user_states, user_foods)
                                        ├── Claude API  (photo/text analysis)
                                        └── Google Sheets API  (meal log)
```

- **Telegram bot** — webhook (not polling), validated via a secret-token header.
- **API Gateway** — HTTP endpoint, throttled (5 req/s, burst 10) as a cost guard.
- **Lambda** — Python 3.12 on arm64 (Graviton); all logic lives here.
- **DynamoDB** — three on-demand tables (see below).
- **Claude API** — `claude-sonnet-4-6` analyzes food from text or images.
- **Google Sheets** — one personal sheet per user; the source of truth for totals.

### DynamoDB tables

| Table | Key | Purpose |
|-------|-----|---------|
| `calorie_bot_user_profile` | `user_id` | Linked sheet + config (timezone, goal), per-day meal counters, rate-limit window, conversation buffer |
| `calorie_bot_user_states` | `user_id` | Transient clarification state; rows self-expire via TTL |
| `calorie_bot_user_foods` | `user_id` + `food_name` | Per-user known foods (macros per 100 g) for reuse |

## How it works

1. **Onboarding** — `/start` returns the service-account email. The user creates a
   Google Sheet, shares it as Editor with that email, and sends the link. The bot
   validates write access and initializes the "Log" headers.
2. **Logging a meal** — send a photo or describe it ("two eggs and toast"). Claude
   estimates calories and macros. If something is ambiguous (portion size, hidden
   sauce), the bot asks one clarifying question — up to 3 rounds — then logs the
   meal and replies with the daily total and calories remaining.
3. **Known foods** — recurring items can be remembered (per 100 g) and reused, so
   the bot scales by portion instead of re-estimating.

Replies mirror the user's own language automatically; intent (log / estimate /
undo / correct / chat) is inferred from phrasing, so plain language usually works
without a command.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Onboarding: get the service-account email and link a sheet |
| `/tz [zone]` | Set timezone (`/tz Europe/Stockholm`) or share location |
| `/goal <kcal>` | Set the daily calorie goal (`/goal 2000`) |
| `/calc <food>` | Estimate calories without logging (alias `/estimate`; also works as a photo caption) |
| `/today` | Today's logged meals and total vs. goal |
| `/undo` | Remove the last logged meal |
| `/model` | Pick the analysis model (Sonnet 4.6 default / Haiku 4.5 cheapest / Opus 4.8 most accurate) via inline buttons |
| `/remember <name> [kcal protein fat carbs]` | Save a known food (per 100 g); omit numbers to let the bot estimate. Works with a labeled photo too |

## Project layout

```
app/                Lambda source
  handler.py        webhook entry point + routing
  claude.py         Claude API calls (text/image analysis, estimates)
  sheets.py         Google Sheets API (append, summaries, undo)
  dynamo.py         DynamoDB access (profile, state, foods, rate limit, counters)
  telegram.py       Telegram Bot API helpers
  geo.py            location → IANA timezone lookup
  secret_store.py   reads secrets from SSM at cold start
  prompts.py        Claude system prompts
template.yaml       AWS SAM infrastructure
samconfig.toml      SAM deploy config (stack: treant-calories-bot, region: eu-central-1)
tests/              pytest suite
docs/SETUP.md       prerequisites + setup checklist
calorie_bot_requirements.md   full spec
```

## Setup

See [docs/SETUP.md](docs/SETUP.md) for the full checklist. In short, you need:

- A Telegram bot token (via [@BotFather](https://t.me/BotFather)) and a webhook secret.
- An Anthropic API key with access to `claude-sonnet-4-6`.
- A Google Cloud service account with the **Sheets API** enabled (`service-account.json`).
- An AWS account with the AWS CLI and AWS SAM CLI installed.

Copy `.env.example` to `.env` and fill in the values.

### Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

### Deploy

Secrets are **not** shipped as plaintext env vars — store them as SSM
SecureString parameters under `/calorie-bot` (see docs/SETUP.md), then:

```bash
sam build
sam deploy
```

After deploy, take the `WebhookUrl` from the stack outputs and register it with
Telegram, passing your webhook secret as `secret_token`.

## Cost

| Component | Cost |
|-----------|------|
| Lambda, DynamoDB, API Gateway | free tier |
| Claude API (~1000 photos/mo) | ~$0.06 |
| Google Sheets API | free |
| **Total** | **~$0.06/mo** |
