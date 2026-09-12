"""
Orion Restaurant Chatbot — Beginner Version
=============================================
This is a simple chatbot for the Orion restaurant (Kyiv).
It receives messages from Instagram Direct and replies using an AI (Claude via Kiro Gateway).

How it works:
1. Instagram sends a POST request to /webhooks/instagram when someone messages the page.
2. We extract the sender ID and message text from the request.
3. We send the text to the AI (Claude) and get a reply.
4. We send the reply back to the user via the Instagram API.

There is also a /demo/chat endpoint so you can test the bot without Instagram.

Run with:  python run.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime

import requests
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response
from pydantic import BaseModel

from token_refresher import refresh_if_needed

# ─────────────────────────────────────────────
# 1. LOAD SETTINGS FROM .env FILE
# ─────────────────────────────────────────────
# python-dotenv reads the .env file and puts the values into os.environ.
# We call this every time we need a fresh value (important if .env changes while running).

def get_env(key: str, default: str = "") -> str:
    """Read a single value from .env file fresh every time (no caching)."""
    load_dotenv(override=True)  # re-read .env on every call
    return os.environ.get(key, default)


# ─────────────────────────────────────────────
# 2. LOGGING SETUP
# ─────────────────────────────────────────────
# We want to see log messages both in the terminal and in a log file.

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),                          # print to terminal
        logging.FileHandler("logs/Orion.log", encoding="utf-8"),  # write to file
    ],
)
log = logging.getLogger("Orion")


# ─────────────────────────────────────────────
# 3. DATABASE SETUP (SQLite)
# ─────────────────────────────────────────────
# SQLite is a simple file-based database — no server needed.
# We store conversation history here so the bot remembers what was said.

DB_PATH = "data/Orion.db"

def init_db():
    """Create the database tables if they don't exist yet."""
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            role        TEXT NOT NULL,   -- 'user' or 'assistant'
            text        TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
    """)
    # conversations table tracks whether a human manager has taken over
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            user_id     TEXT PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'bot',  -- 'bot' or 'human'
            updated_at  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    log.info("Database ready at %s", DB_PATH)


def save_message(user_id: str, role: str, text: str):
    """Save one message (from user or from bot) to the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (id, user_id, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, role, text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_history(user_id: str, limit: int = 8) -> list[dict]:
    """
    Get the last N messages for a user as a list of dicts like:
    [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi!"}]
    This is the format the AI expects.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, text FROM messages WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    # rows are newest-first, so we reverse them to get oldest-first
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]


def is_human_controlled(user_id: str) -> bool:
    """Return True if a human manager has taken over this conversation."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT status FROM conversations WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return bool(row and row[0] == "human")


def set_conversation_status(user_id: str, status: str):
    """Set conversation status to 'bot' or 'human'."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO conversations (user_id, status, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
        (user_id, status, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# 4. KNOWLEDGE BASE (YAML file with menu/info)
# ─────────────────────────────────────────────
# The menu and restaurant info live in a YAML file.
# The admin can edit it without touching any code.

_BASE = os.path.dirname(os.path.abspath(__file__))
KB_PATH = os.path.join(_BASE, ".kiro/specs/restaurant-ai-assistant/knowledge-base.yaml")
SYSTEM_PROMPT_PATH = os.path.join(_BASE, ".kiro/specs/restaurant-ai-assistant/system-prompt.md")

def load_knowledge_base() -> str:
    """
    Load the knowledge base YAML and return it as a text string.
    This text will be injected into the AI system prompt so the AI knows the menu.
    """
    try:
        with open(KB_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # Convert back to YAML text — clean and readable for the AI
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    except OSError:
        log.warning("Knowledge base file not found: %s", KB_PATH)
        return "No menu data available."


def build_system_prompt() -> str:
    """
    Build the full system prompt for the AI.
    It reads the base prompt from a file and inserts the knowledge base into it.
    """
    try:
        with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
            base = f.read()
    except OSError:
        base = "You are the Orion restaurant assistant. Be helpful and concise. {{KNOWLEDGE_BASE}}"

    kb_text = load_knowledge_base()
    # Replace the placeholder with the actual menu data
    return base.replace("{{KNOWLEDGE_BASE}}", kb_text)


# ─────────────────────────────────────────────
# 5. AI (LLM) CALL via Kiro Gateway
# ─────────────────────────────────────────────
# Kiro Gateway is a local proxy that gives us access to Claude (AI model).
# It speaks the OpenAI API format, so we send messages the same way as OpenAI.

def ask_ai(user_text: str, history: list[dict]) -> str | None:
    """
    Send a message to the AI and get a reply.

    Parameters:
        user_text: the latest message from the user
        history:   list of previous messages (for context)

    Returns:
        The AI's reply as a string, or None if something went wrong.
    """
    gateway_url = get_env("KIRO_GATEWAY_URL")
    api_key = get_env("KIRO_GATEWAY_API_KEY")
    model = get_env("KIRO_GATEWAY_MODEL", "claude-haiku-4.5")

    # If no gateway URL is set, we can't use the AI
    if not gateway_url or not api_key:
        log.info("AI not configured — running in offline mode")
        return None

    url = gateway_url.rstrip("/") + "/v1/chat/completions"

    # Build the messages list: system prompt + history + new user message
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,   # lower = more predictable answers
        "tool_choice": "none", # Kiro Gateway requires this
    }

    try:
        t0 = time.perf_counter()
        resp = requests.post(url, headers=headers, json=body, timeout=20)
        ms = (time.perf_counter() - t0) * 1000
        log.info("AI responded in %.0f ms (status %d)", ms, resp.status_code)
        resp.raise_for_status()

        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        if not content or not content.strip():
            log.warning("AI returned empty content")
            return None

        # Some models wrap their answer in <think>...</think> tags — strip them
        if "</think>" in content:
            content = content[content.rfind("</think>") + 8:].strip()

        # Some models wrap JSON in ```json ... ``` — strip those fences too
        if content.startswith("```"):
            content = content[content.find("\n") + 1:]
            content = content[:content.rfind("```")].strip()

        # The AI might return a JSON object (from the advanced version's prompt)
        # or plain text. Try to extract the "reply" field if it's JSON.
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "reply" in parsed:
                return parsed["reply"]
        except (json.JSONDecodeError, ValueError):
            pass  # not JSON — use the content as-is

        return content

    except requests.RequestException as exc:
        log.error("AI call failed: %s", exc)
        return None


# ─────────────────────────────────────────────
# 6. RULE-BASED FALLBACK (works without AI)
# ─────────────────────────────────────────────
# When the AI is not available, we use simple keyword matching.
# This keeps the demo working even without a Kiro Gateway connection.

def rule_based_reply(text: str) -> str:
    """
    Return a hardcoded reply based on keywords in the user's message.
    This is the fallback when the AI is not configured or fails.
    """
    lowered = text.lower()

    # Check for Ukrainian characters to decide the reply language
    has_cyrillic = any("\u0400" <= c <= "\u04FF" for c in text)

    if any(w in lowered for w in ("менеджер", "людин", "manager", "human", "оператор")):
        return ("Передаю ваш запит менеджеру. Також можна зателефонувати: +380 (98) 724-23-24"
                if has_cyrillic else
                "I'll pass your request to a manager. You can also call: +380 (98) 724-23-24")

    if any(w in lowered for w in ("меню", "menu", "страв", "ціна", "price")):
        return ("У нас є: хоспер-меню (м'ясо та риба на вугіллі), ковбаски власного виробництва, "
                "понад 11 сортів пива, домашні настоянки. Що вас цікавить?"
                if has_cyrillic else
                "We have: hosper menu (charcoal meat & fish), house-made sausages, "
                "11+ draft beers, homemade infusions. What interests you?")

    if any(w in lowered for w in ("пиво", "beer")):
        return ("У нас понад 11 сортів розливного пива — німецькі, чеські, крафтові та інші. "
                "Хочете дізнатися більше?"
                if has_cyrillic else
                "We have 11+ draft beers — German, Czech, craft and more. Want to know more?")

    if any(w in lowered for w in ("настоянк", "infusion")):
        return ("Домашні настоянки: Обліпихівка, Малинівка, Вишнівка, Хріновуха, Медовуха. "
                "Яка вас цікавить?"
                if has_cyrillic else
                "Homemade infusions: sea-buckthorn, raspberry, cherry, horseradish, honey. "
                "Which one interests you?")

    if any(w in lowered for w in ("броню", "стіл", "reserv", "table")):
        return ("Бронювання столу — за телефоном адміністратора: +380 (98) 724-23-24"
                if has_cyrillic else
                "Table reservations by phone: +380 (98) 724-23-24")

    # Default greeting
    return ("Вітаю! Це Orion 🔥 Чим можу допомогти: меню, замовлення чи бронювання?"
            if has_cyrillic else
            "Hi! This is Orion 🔥 How can I help: menu, an order, or a reservation?")


# ─────────────────────────────────────────────
# 7. PROCESS ONE MESSAGE (main logic)
# ─────────────────────────────────────────────

def process_message(user_id: str, text: str) -> str:
    """
    Main function: takes a user message, returns the bot's reply.

    Steps:
    1. Check if a human manager has taken over — if so, stay silent.
    2. Load conversation history from the database.
    3. Try to get a reply from the AI.
    4. If AI fails, use the rule-based fallback.
    5. Save both messages to the database.
    6. Return the reply.
    """
    log.info("IN  user=%s text=%r", user_id, text)

    # Step 1: If a human manager is handling this conversation, bot stays silent
    if is_human_controlled(user_id):
        log.info("Conversation %s is human-controlled — bot silent", user_id)
        return ""

    # Step 2: Load the last 8 messages as context for the AI
    history = get_history(user_id, limit=8)

    # Step 3: Ask the AI
    reply = ask_ai(text, history)

    # Step 4: If AI didn't work, use simple keyword rules
    if not reply:
        reply = rule_based_reply(text)
        log.info("Using rule-based fallback")

    # Step 5: Save both messages to the database
    save_message(user_id, "user", text)
    save_message(user_id, "assistant", reply)

    log.info("OUT reply=%r", reply[:120])
    return reply


# ─────────────────────────────────────────────
# 8. INSTAGRAM API — SEND A MESSAGE
# ─────────────────────────────────────────────
# To reply to a user on Instagram, we call the Meta Graph API.

GRAPH_API_URL = "https://graph.facebook.com/v21.0/me/messages"

def send_instagram_message(recipient_id: str, text: str):
    """
    Send a text message to an Instagram user.

    Parameters:
        recipient_id: the Instagram user's ID (a number like "2713968769128968")
        text:         the message to send
    """
    # Read the token fresh every time (it might have been updated in .env)
    token = get_env("IG_PAGE_ACCESS_TOKEN")

    if not token:
        # No token configured — just log the message (useful for local testing)
        log.info("[Instagram offline] -> %s: %s", recipient_id, text)
        return

    log.info("Sending Instagram message to %s (token prefix: %s...)", recipient_id, token[:20])

    body = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    }

    try:
        resp = requests.post(
            GRAPH_API_URL,
            params={"access_token": token},
            json=body,
            timeout=10,
        )
        log.info("Instagram API response: status=%d body=%s", resp.status_code, resp.text)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to send Instagram message: %s", exc)


# ─────────────────────────────────────────────
# 9. FASTAPI APPLICATION
# ─────────────────────────────────────────────
# FastAPI is a web framework. It listens for HTTP requests and calls our functions.

app = FastAPI(title="Orion Chatbot (Beginner)", version="1.0")


async def _token_refresh_loop():
    """Background task: re-check token expiry every 24 hours."""
    while True:
        await asyncio.sleep(24 * 60 * 60)  # wait 24 hours
        try:
            refresh_if_needed()
        except Exception as exc:
            log.error("Token refresh loop error: %s", exc)


@app.on_event("startup")
async def startup():
    """This runs once when the server starts."""
    init_db()
    # Check token expiry right away at startup
    try:
        refresh_if_needed()
    except Exception as exc:
        log.error("Startup token refresh failed: %s", exc)
    # Schedule the 24-hour background check
    asyncio.create_task(_token_refresh_loop())
    log.info("Orion chatbot started!")


@app.get("/health")
def health():
    """Simple health check — visit http://localhost:8000/health to see if it's running."""
    return {
        "status": "ok",
        "ai_enabled": bool(get_env("KIRO_GATEWAY_URL") and get_env("KIRO_GATEWAY_API_KEY")),
        "instagram_enabled": bool(get_env("IG_PAGE_ACCESS_TOKEN")),
    }


# ─────────────────────────────────────────────
# 9a. DEMO CHAT ENDPOINT
# ─────────────────────────────────────────────
# Use this to test the bot without Instagram.
# Example: POST http://localhost:8000/demo/chat  {"text": "Hello!"}

class ChatRequest(BaseModel):
    user_id: str = "demo-user"   # you can use any ID for testing
    text: str


@app.post("/demo/chat")
def demo_chat(req: ChatRequest):
    """Test the chatbot directly without Instagram."""
    reply = process_message(req.user_id, req.text)
    return {"reply": reply}


# ─────────────────────────────────────────────
# 9b. INSTAGRAM WEBHOOK
# ─────────────────────────────────────────────
# Meta (Instagram) sends a GET request to verify the webhook URL.
# Then it sends POST requests whenever someone messages the page.

@app.get("/webhooks/instagram")
def verify_webhook(
    mode: str = Query(default="", alias="hub.mode"),
    token: str = Query(default="", alias="hub.verify_token"),
    challenge: str = Query(default="", alias="hub.challenge"),
):
    """
    Meta calls this to verify that we own this URL.
    We check the token matches what we set in Meta Developer Console,
    then echo back the challenge string.
    """
    verify_token = get_env("META_VERIFY_TOKEN", "Orion_verify_token")

    if mode == "subscribe" and token == verify_token:
        log.info("Instagram webhook verified successfully")
        return Response(content=challenge, media_type="text/plain")

    log.warning("Instagram webhook verification failed (token mismatch)")
    return Response(content="verification failed", status_code=403)


@app.post("/webhooks/instagram")
async def instagram_webhook(request: Request):
    """
    Meta calls this whenever someone sends a message to the Instagram page.
    We parse the payload, process the message, and send a reply.
    """
    payload = await request.json()
    log.info("Instagram webhook received:\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

    # The payload can contain multiple entries and multiple messages
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")

            # Handle both regular messages and edited messages
            msg = event.get("message") or event.get("message_edit")

            if not msg or not sender_id:
                continue  # skip events without a message

            text = msg.get("text", "")

            # Skip empty messages and echoes (copies of our own sent messages)
            if not text or msg.get("is_echo"):
                continue

            # Process the message and get a reply
            reply = process_message(sender_id, text)

            # Send the reply back to the user (empty reply = bot is silent)
            if reply:
                send_instagram_message(sender_id, reply)

    return {"status": "ok"}


# ─────────────────────────────────────────────
# 9c. MANAGER HANDOFF ENDPOINTS
# ─────────────────────────────────────────────
# These let a human manager take over or release a conversation.

# ─────────────────────────────────────────────
# 9d. ADMIN ENV VARS PAGE
# ─────────────────────────────────────────────

ADMIN_HTML = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Orion - Env Vars</title>
<style>
  body{font-family:sans-serif;max-width:700px;margin:40px auto;padding:0 16px;background:#f5f5f5}
  h1{font-size:1.3rem;margin-bottom:24px}
  .row{display:flex;gap:8px;margin-bottom:10px;align-items:center}
  .key{width:260px;font-size:.85rem;font-weight:600;color:#333;flex-shrink:0;word-break:break-all}
  input[type=text]{flex:1;padding:6px 8px;border:1px solid #ccc;border-radius:4px;font-size:.9rem}
  button{padding:8px 20px;background:#0066cc;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:.95rem}
  button:hover{background:#0052a3}
  .msg{margin-top:16px;padding:10px;border-radius:4px;display:none}
  .ok{background:#d4edda;color:#155724}
  .err{background:#f8d7da;color:#721c24}
  .secret input{color:transparent;text-shadow:0 0 6px #333}
  .secret input:focus{color:#000;text-shadow:none}
</style></head><body>
<h1>Orion - Environment Variables</h1>
<form id="f"></form>
<br><button onclick="save()">Save all</button>
<div class="msg" id="msg"></div>
<script>
const SECRET_KEYS=['KIRO_GATEWAY_API_KEY','META_APP_SECRET','META_LONG_LIVED_USER_TOKEN','IG_PAGE_ACCESS_TOKEN','RENDER_API_KEY'];
const pwd=new URLSearchParams(window.location.search).get('password')||'';
async function load(){
  const r=await fetch('/admin/env/data?password='+pwd);
  if(!r.ok){document.getElementById('f').innerHTML='<p style="color:red">Failed to load: '+r.status+'</p>';return;}
  const vars=await r.json();
  const f=document.getElementById('f');
  vars.forEach(function(v){
    const key=v.envVar?v.envVar.key:v.key;
    const val=v.envVar?v.envVar.value:v.value;
    const isSecret=SECRET_KEYS.indexOf(key)>=0;
    const row=document.createElement('div');
    row.className='row';
    const d=document.createElement('div');
    d.className='key';
    d.textContent=key;
    const inp=document.createElement('input');
    inp.type='text';
    inp.name=key;
    inp.value=val;
    inp.dataset.val=val;
    inp.style.color='transparent';
    inp.style.textShadow='0 0 6px #333';
    inp.addEventListener('focus',function(){this.style.color='';this.style.textShadow='';});
    inp.addEventListener('blur',function(){if(this.value===this.dataset.val){this.style.color='transparent';this.style.textShadow='0 0 6px #333';}});
    row.appendChild(d);
    row.appendChild(inp);
    f.appendChild(row);
  });
}
async function save(){
  const inputs=document.querySelectorAll('#f input');
  const data={};
  inputs.forEach(function(i){data[i.name]=i.value;});
  const r=await fetch('/admin/env/save?password='+pwd,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const msg=document.getElementById('msg');
  msg.style.display='block';
  if(r.ok){msg.className='msg ok';msg.textContent='Saved successfully';}
  else{msg.className='msg err';msg.textContent='Save failed: '+r.status;}
}
load();
</script></body></html>
"""


@app.get("/admin/env")
def admin_env_page(password: str = Query(default="")):
    """Admin page to view and edit Render environment variables."""
    admin_password = get_env("ADMIN_PASSWORD", "")
    if not admin_password or password != admin_password:
        return Response(content="Unauthorized — add ?password=YOUR_ADMIN_PASSWORD to the URL", status_code=401)
    return Response(content=ADMIN_HTML, media_type="text/html")


@app.get("/admin/env/data")
def admin_env_data(password: str = Query(default="")):
    """Return current Render env vars as JSON."""
    admin_password = get_env("ADMIN_PASSWORD", "")
    if not admin_password or password != admin_password:
        return Response(status_code=401)
    api_key = get_env("RENDER_API_KEY")
    service_id = get_env("RENDER_SERVICE_ID")
    if not api_key or not service_id:
        return Response(content="RENDER_API_KEY / RENDER_SERVICE_ID not set", status_code=500)
    resp = requests.get(
        f"https://api.render.com/v1/services/{service_id}/env-vars",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


@app.post("/admin/env/save")
async def admin_env_save(request: Request, password: str = Query(default="")):
    """Save updated env vars to Render."""
    admin_password = get_env("ADMIN_PASSWORD", "")
    if not admin_password or password != admin_password:
        return Response(status_code=401)
    api_key = get_env("RENDER_API_KEY")
    service_id = get_env("RENDER_SERVICE_ID")
    if not api_key or not service_id:
        return Response(content="RENDER_API_KEY / RENDER_SERVICE_ID not set", status_code=500)
    data = await request.json()
    env_vars = [{"key": k, "value": v} for k, v in data.items()]
    resp = requests.put(
        f"https://api.render.com/v1/services/{service_id}/env-vars",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        json=env_vars,
        timeout=10,
    )
    resp.raise_for_status()
    # update in-process too
    for k, v in data.items():
        os.environ[k] = v
    return {"status": "ok"}


@app.post("/manager/handoff/{user_id}")
def manager_take_over(user_id: str):
    """Manager takes over — bot goes silent for this user."""
    set_conversation_status(user_id, "human")
    log.info("Manager took over conversation for user %s", user_id)
    return {"status": "human"}


@app.post("/manager/release/{user_id}")
def manager_release(user_id: str):
    """Manager releases — bot resumes answering for this user."""
    set_conversation_status(user_id, "bot")
    log.info("Bot resumed for user %s", user_id)
    return {"status": "bot"}
