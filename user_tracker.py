# user_tracker.py
# Appends a new row to a Google Sheet tab on every user message.
# Fetches Instagram profile info (username, followers, verified) via Graph API.

import logging
import os
import json
import tempfile
from datetime import datetime, timezone
from threading import Thread

import requests
from dotenv import load_dotenv

log = logging.getLogger("Orion")

GRAPH_API_URL = "https://graph.facebook.com/v21.0"


def _get_env(key: str) -> str:
    load_dotenv(override=True)
    return os.environ.get(key, "")


def _get_instagram_profile(user_id: str) -> dict:
    """Fetch username, follower_count, is_verified_user from Instagram Graph API."""
    token = _get_env("IG_PAGE_ACCESS_TOKEN")
    if not token:
        return {}
    try:
        r = requests.get(
            f"{GRAPH_API_URL}/{user_id}",
            params={"fields": "username,follower_count,is_verified_user,profile_pic", "access_token": token},
            timeout=10,
        )
        data = r.json()
        if "error" in data:
            log.warning("[user_tracker] Graph API error: %s", data["error"].get("message"))
            return {}
        return data
    except Exception as e:
        log.warning("[user_tracker] Failed to fetch Instagram profile: %s", e)
        return {}


def _get_client(creds_json: str, creds_path: str):
    import gspread
    if creds_json:
        info = json.loads(creds_json)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(info, f)
            tmp_path = f.name
        gc = gspread.service_account(filename=tmp_path)
        os.unlink(tmp_path)
    elif creds_path:
        gc = gspread.service_account(filename=creds_path)
    else:
        return None
    return gc


def _track(sender: str, sheet_tab: str):
    try:
        sheet_id  = _get_env("GOOGLE_SHEET_ID")
        creds_json = _get_env("GOOGLE_CREDS_JSON")
        creds_path = _get_env("GOOGLE_CREDS_PATH")

        if not sheet_id or (not creds_json and not creds_path):
            return

        # Fetch Instagram profile
        profile = _get_instagram_profile(sender)
        username     = profile.get("username", "")
        followers    = profile.get("follower_count", "")
        verified     = profile.get("is_verified_user", "")
        profile_pic  = profile.get("profile_pic", "")

        gc = _get_client(creds_json, creds_path)
        if gc is None:
            return

        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet(sheet_tab)

        # Write header if sheet is empty
        if ws.cell(1, 1).value != "user_id":
            ws.insert_row(
                ["user_id", "username", "followers", "verified", "profile_pic", "timestamp"],
                index=1
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        ws.append_row([sender, username, followers, verified, profile_pic, now])
        log.info("[user_tracker] Row added: %s (@%s) followers=%s", sender, username, followers)

    except Exception as e:
        log.warning("[user_tracker] Failed: %s", e)


def track_user(sender: str, sheet_tab: str = "Orion"):
    """Append a row to the given Google Sheet tab (runs in background thread)."""
    Thread(target=_track, args=(sender, sheet_tab), daemon=True).start()
