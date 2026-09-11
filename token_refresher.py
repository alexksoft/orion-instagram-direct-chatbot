"""
token_refresher.py — Automatic Instagram Token Refresh
=======================================================
Instagram Page Access Tokens expire after 60 days.
This module refreshes them automatically so the bot never stops working.

How it works:
1. Read META_APP_ID, META_APP_SECRET, META_LONG_LIVED_USER_TOKEN from .env
2. Ask Meta how many days are left on the token (debug_token API)
3. If fewer than 10 days remain → exchange for a fresh 60-day token
4. Fetch a new Page Access Token using the refreshed User Token
5. Write both new tokens back to .env

Call refresh_if_needed() at startup and then once every 24 hours.

What you need in .env:
    META_APP_ID=your_app_id
    META_APP_SECRET=your_app_secret
    META_LONG_LIVED_USER_TOKEN=the_60_day_user_token
    IG_PAGE_ACCESS_TOKEN=the_page_token  ← this gets updated automatically
"""

import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger("Orion.token_refresher")

# Refresh when fewer than this many days remain on the token
REFRESH_THRESHOLD_DAYS = 10

# Meta Graph API base URL
GRAPH = "https://graph.facebook.com/v26.0"

# Path to the .env file (same folder as this script)
ENV_FILE = Path(".env")


# ─────────────────────────────────────────────
# .env FILE HELPERS
# ─────────────────────────────────────────────

def _read_env() -> dict[str, str]:
    """
    Read the .env file and return all key=value pairs as a dictionary.
    Lines starting with # are comments and are ignored.
    """
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _write_env_key(key: str, value: str) -> None:
    """
    Update a single key=value line in .env.
    If the key already exists → replace its value.
    If the key doesn't exist → add it at the end.
    """
    if not ENV_FILE.exists():
        # Create the file if it doesn't exist yet
        ENV_FILE.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    text = ENV_FILE.read_text(encoding="utf-8")

    # Look for an existing line like: KEY=anything
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        # Replace the existing line
        text = pattern.sub(f"{key}={value}", text)
    else:
        # Append a new line at the end
        text = text.rstrip("\n") + f"\n{key}={value}\n"

    ENV_FILE.write_text(text, encoding="utf-8")


# ─────────────────────────────────────────────
# STEP 0: CHECK HOW MANY DAYS ARE LEFT
# ─────────────────────────────────────────────

def _days_remaining(user_token: str, app_id: str, app_secret: str) -> float | None:
    """
    Ask Meta's debug_token API how many seconds are left on the token.
    Returns the number of days remaining, or None if the check fails.

    The access_token for this call is "APP_ID|APP_SECRET" (app-level token).
    """
    try:
        resp = requests.get(
            f"{GRAPH}/debug_token",
            params={
                "input_token": user_token,
                "access_token": f"{app_id}|{app_secret}",  # app-level token
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        expires_at = data.get("expires_at")  # Unix timestamp (seconds since 1970)

        if not expires_at:
            # expires_at = 0 means the token never expires (rare but possible)
            log.info("Token has no expiry date — skipping refresh")
            return None

        seconds_left = expires_at - time.time()
        days_left = seconds_left / 86400  # 86400 seconds in a day
        log.info("Token expires in %.1f days", days_left)
        return days_left

    except Exception as exc:
        log.warning("Could not check token expiry: %s", exc)
        return None


# ─────────────────────────────────────────────
# STEP 1: REFRESH THE LONG-LIVED USER TOKEN
# ─────────────────────────────────────────────

def _refresh_user_token(old_token: str, app_id: str, app_secret: str) -> str | None:
    """
    Exchange the current Long-Lived User Token for a fresh 60-day one.

    Meta endpoint:
        GET /oauth/access_token
            ?grant_type=fb_exchange_token
            &client_id=APP_ID
            &client_secret=APP_SECRET
            &fb_exchange_token=OLD_TOKEN

    Returns the new token string, or None if the request failed.
    """
    try:
        resp = requests.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": old_token,
            },
            timeout=10,
        )
        resp.raise_for_status()
        new_token = resp.json().get("access_token")
        if new_token:
            log.info("User token refreshed successfully")
        return new_token
    except Exception as exc:
        log.error("User token refresh failed: %s", exc)
        return None


# ─────────────────────────────────────────────
# STEP 2: GET A NEW PAGE ACCESS TOKEN
# ─────────────────────────────────────────────

def _get_page_token(user_token: str) -> str | None:
    """
    Fetch the Page Access Token using the freshly refreshed User Token.

    Meta endpoint:
        GET /me/accounts
            ?fields=access_token
            &access_token=USER_TOKEN

    Returns the first page's access_token, or None if the request failed.
    """
    try:
        resp = requests.get(
            f"{GRAPH}/me/accounts",
            params={
                "fields": "access_token",
                "access_token": user_token,
            },
            timeout=10,
        )
        resp.raise_for_status()
        pages = resp.json().get("data", [])
        if not pages:
            log.warning("No pages found for this user token")
            return None
        token = pages[0].get("access_token")
        if token:
            log.info("Page token fetched successfully")
        return token
    except Exception as exc:
        log.error("Page token fetch failed: %s", exc)
        return None


# ─────────────────────────────────────────────
# MAIN FUNCTION — call this at startup and every 24 hours
# ─────────────────────────────────────────────

def refresh_if_needed() -> None:
    """
    Check token expiry and refresh if fewer than REFRESH_THRESHOLD_DAYS remain.

    Reads credentials from .env.
    Writes updated tokens back to .env when refresh happens.
    Safe to call multiple times — does nothing if token is still fresh.
    """
    # Read the required values from .env
    env = _read_env()
    app_id = env.get("META_APP_ID", "")
    app_secret = env.get("META_APP_SECRET", "")
    user_token = env.get("META_LONG_LIVED_USER_TOKEN", "")

    # If any of the three required values are missing, skip silently
    if not all([app_id, app_secret, user_token]):
        log.info(
            "Token auto-refresh skipped: META_APP_ID / META_APP_SECRET / "
            "META_LONG_LIVED_USER_TOKEN not set in .env"
        )
        return

    # Step 0: check how many days are left
    days = _days_remaining(user_token, app_id, app_secret)

    # If we couldn't check, or there's still plenty of time — do nothing
    if days is not None and days >= REFRESH_THRESHOLD_DAYS:
        log.info("Token still valid (%.1f days left) — no refresh needed", days)
        return

    log.info("Refreshing Instagram tokens (%.1f days left)…", days if days is not None else 0)

    # Step 1: get a fresh 60-day User Token
    new_user_token = _refresh_user_token(user_token, app_id, app_secret)
    if not new_user_token:
        log.error("Refresh aborted — could not get new user token")
        return

    # Step 2: get a new Page Access Token
    new_page_token = _get_page_token(new_user_token)
    if not new_page_token:
        log.error("Refresh aborted — could not get new page token")
        return

    # Step 3: save both tokens to .env so they survive a restart
    _write_env_key("META_LONG_LIVED_USER_TOKEN", new_user_token)
    _write_env_key("IG_PAGE_ACCESS_TOKEN", new_page_token)
    log.info("Both tokens written to .env — refresh complete ✓")
