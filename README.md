# Orion Restaurant Chatbot — Beginner Version

A simple Instagram chatbot for the Orion restaurant (Kyiv).
Written for beginners: **one file**, lots of comments, no complex abstractions.

## How it works

```
Instagram user sends a message
        ↓
Meta sends POST to /webhooks/instagram
        ↓
We extract sender ID + message text
        ↓
We ask Claude AI (via Kiro Gateway) for a reply
        ↓
If AI fails → use simple keyword rules
        ↓
We send the reply back via Instagram API
```

## Project structure

```
main.py              ← THE WHOLE APP IS HERE (one file, heavily commented)
run.py               ← Starts the server (fixes Windows event loop bug)
start.bat            ← Starts server + Cloudflare tunnel (Windows)
requirements.txt     ← Python packages needed
.env.example         ← Copy to .env and fill in your keys
data/                ← SQLite database (created automatically)
logs/                ← Log files (created automatically)
.kiro/specs/restaurant-ai-assistant/
    knowledge-base.yaml   ← Menu, prices, contacts (edit this, no code change needed)
    system-prompt.md      ← Instructions for the AI
```

## Quick start

```bash
# 1. Create virtual environment
python -m venv .venv

# 2. Activate it (Windows)
.venv\Scripts\activate

# 3. Install packages
pip install -r requirements.txt

# 4. Copy config and fill in your keys
copy .env.example .env

# 5. Start the server
python run.py
```

Then open http://localhost:8000/docs to see the API.

## Test without Instagram

```bash
curl -X POST http://localhost:8000/demo/chat \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"What beer do you have?\"}"
```

## Connect to Instagram

1. Run `start.bat` — it opens the server and a Cloudflare tunnel
2. Copy the tunnel URL (e.g. `https://abc-123.trycloudflare.com`)
3. In Meta Developer Console → Webhooks, set callback URL to:
   `https://abc-123.trycloudflare.com/webhooks/instagram`
4. Set verify token to `Orion_verify_token` (or whatever is in your `.env`)

**Note:** The tunnel URL changes every restart — update Meta each time.

## Difference from the advanced version

| Feature | Beginner | Advanced |
|---------|----------|----------|
| Files | 1 file | 15+ files |
| Cart / order tracking | No | Yes |
| Telegram staff notifications | No | Yes |
| Language detection | Simple (Cyrillic check) | Full heuristic |
| Code style | Flat functions, lots of comments | Classes, modules |

## Manager handoff

If you want to take over a conversation manually:

```bash
# Take over (bot goes silent)
curl -X POST http://localhost:8000/manager/handoff/USER_ID

# Release (bot resumes)
curl -X POST http://localhost:8000/manager/release/USER_ID
```

Replace `USER_ID` with the Instagram sender ID (e.g. `2713968769128968`).
