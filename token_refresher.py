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
import os
import time

import requests

log = logging.getLogger("Orion.token_refresher")

REFRESH_THRESHOLD_DAYS = 10
GRAPH = "https://graph.facebook.com/v26.0"
RENDER_API = "https://api.render.com/v1"


def _update_render_env(key: str, value: str) -> None:
    """
    Update an environment variable on Render via API so it survives restarts.
    Requires RENDER_API_KEY and RENDER_SERVICE_ID set in Render dashboard.
    Also updates os.environ so the running process sees the new value immediately.
    """
    os.environ[key] = value  # update in-process immediately

    api_key = os.environ.get("RENDER_API_KEY", "")
    service_id = os.environ.get("RENDER_SERVICE_ID", "")
    if not api_key or not service_id:
        log.warning("RENDER_API_KEY / RENDER_SERVICE_ID not set — token updated in memory only")
        return

    try:
        # Render API: retrieve current env vars, patch the target key, PUT back the full list
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        resp = requests.get(f"{RENDER_API}/services/{service_id}/env-vars", headers=headers, timeout=10)
        resp.raise_for_status()
        env_vars = resp.json()  # list of {"key": ..., "value": ...}

        updated = False
        for item in env_vars:
            if item.get("key") == key:
                item["value"] = value
                updated = True
                break
        if not updated:
            env_vars.append({"key": key, "value": value})

        put_resp = requests.put(
            f"{RENDER_API}/services/{service_id}/env-vars",
            headers={**headers, "Content-Type": "application/json"},
            json=env_vars,
            timeout=10,
        )
        put_resp.raise_for_status()
        log.info("Render env var %s updated via API", key)
    except Exception as exc:
        log.error("Failed to update Render env var %s: %s", key, exc)


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
    app_id = os.environ.get("META_APP_ID", "")
    app_secret = os.environ.get("META_APP_SECRET", "")
    user_token = os.environ.get("META_LONG_LIVED_USER_TOKEN", "")

    if not all([app_id, app_secret, user_token]):
        log.info(
            "Token auto-refresh skipped: META_APP_ID / META_APP_SECRET / "
            "META_LONG_LIVED_USER_TOKEN not set"
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

    _update_render_env("META_LONG_LIVED_USER_TOKEN", new_user_token)
    _update_render_env("IG_PAGE_ACCESS_TOKEN", new_page_token)
    log.info("Both tokens saved to Render env vars — refresh complete ✓")
