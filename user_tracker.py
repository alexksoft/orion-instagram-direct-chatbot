# user_tracker.py
# Appends a new row to a Google Sheet tab on every user message.

import logging
import os
from datetime import datetime, timezone
from threading import Thread

from dotenv import load_dotenv

log = logging.getLogger("Orion")

_PREFIX_MAP = {
    "+1":    ("USA/Canada",        "America/New_York"),
    "+44":   ("UK",                "Europe/London"),
    "+49":   ("Germany",           "Europe/Berlin"),
    "+33":   ("France",            "Europe/Paris"),
    "+34":   ("Spain",             "Europe/Madrid"),
    "+39":   ("Italy",             "Europe/Rome"),
    "+48":   ("Poland",            "Europe/Warsaw"),
    "+380":  ("Ukraine",           "Europe/Kyiv"),
    "+7":    ("Russia/Kazakhstan", "Europe/Moscow"),
    "+375":  ("Belarus",           "Europe/Minsk"),
    "+90":   ("Turkey",            "Europe/Istanbul"),
    "+972":  ("Israel",            "Asia/Jerusalem"),
    "+971":  ("UAE",               "Asia/Dubai"),
    "+966":  ("Saudi Arabia",      "Asia/Riyadh"),
    "+20":   ("Egypt",             "Africa/Cairo"),
    "+27":   ("South Africa",      "Africa/Johannesburg"),
    "+234":  ("Nigeria",           "Africa/Lagos"),
    "+91":   ("India",             "Asia/Kolkata"),
    "+86":   ("China",             "Asia/Shanghai"),
    "+81":   ("Japan",             "Asia/Tokyo"),
    "+82":   ("South Korea",       "Asia/Seoul"),
    "+65":   ("Singapore",         "Asia/Singapore"),
    "+62":   ("Indonesia",         "Asia/Jakarta"),
    "+55":   ("Brazil",            "America/Sao_Paulo"),
    "+54":   ("Argentina",         "America/Argentina/Buenos_Aires"),
    "+52":   ("Mexico",            "America/Mexico_City"),
    "+61":   ("Australia",         "Australia/Sydney"),
}


def _get_country_tz(sender: str) -> tuple[str, str]:
    phone = sender if sender.startswith("+") else "+" + sender
    for length in (4, 3, 2):
        prefix = phone[:length]
        if prefix in _PREFIX_MAP:
            return _PREFIX_MAP[prefix]
    return ("Unknown", "Unknown")


def _get_env(key: str) -> str:
    load_dotenv(override=True)
    return os.environ.get(key, "")


def _track(sender: str, sheet_tab: str):
    try:
        import gspread
        import json
        import tempfile
        from google.oauth2.service_account import Credentials

        sheet_id = _get_env("GOOGLE_SHEET_ID")
        if not sheet_id:
            return

        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

        creds_path = _get_env("GOOGLE_CREDS_PATH")
        creds_json = _get_env("GOOGLE_CREDS_JSON")

        if creds_json:
            # Write to a temp file to avoid any escaping issues
            info = json.loads(creds_json)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                json.dump(info, f)
                tmp_path = f.name
            gc = gspread.service_account(filename=tmp_path)
            os.unlink(tmp_path)
        elif creds_path:
            gc = gspread.service_account(filename=creds_path)
        else:
            return
        sh = gc.open_by_key(sheet_id)
        ws = sh.worksheet(sheet_tab)

        # Write header if sheet is empty
        if ws.cell(1, 1).value != "user_id":
            ws.insert_row(["user_id", "country", "timezone", "timestamp"], index=1)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        country, tz = _get_country_tz(sender)
        ws.append_row([sender, country, tz, now])
        log.info("[user_tracker] Row added for %s (%s) on tab '%s'", sender, country, sheet_tab)

    except Exception as e:
        log.warning("[user_tracker] Failed: %s", e)


def track_user(sender: str, sheet_tab: str = "Orion"):
    """Append a row to the given Google Sheet tab (runs in background thread)."""
    Thread(target=_track, args=(sender, sheet_tab), daemon=True).start()
