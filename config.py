"""
Settings for the Household Watch Concierge.

Everything tunable lives here and can be overridden from .env, so the other
modules never read environment variables directly.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def require(name: str) -> str:
    """Read a required setting, with a clear message instead of a bare KeyError."""
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing setting {name}. Put it in .env (see .env.example) or set it in your shell.")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"Setting {name} must be a whole number, got {raw!r}.")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# ---------------------------------------------------------------- TV and APIs
# TV_IP / TV_MAC are read with require() at the moment they are needed (tv.py),
# so lookup-only commands still work without TV settings.
KEY_FILE = BASE_DIR / ".webos_key"   # pairing key so the TV doesn't ask every time
COUNTRY = "in"                       # country code for streaming availability lookups

# ---------------------------------------------------------------- App IDs on this TV
# (from `python cli.py apps`)
NETFLIX_APP = "netflix"
PRIME_APP = "amazon"
YOUTUBE_APP = "youtube.leanback.v4"

# ---------------------------------------------------------------- Which service wins
# When a title is on several services, the first one in this list is used.
SERVICE_PREFERENCE = _list("SERVICE_PREFERENCE", "netflix,prime")

# ---------------------------------------------------------------- Delays (seconds)
# If a step fires too early, raise these first.
POST_WAKE_WAIT = _int("POST_WAKE_WAIT", 5)                  # settle time once the TV answers after wake

NETFLIX_PROFILE_WAIT = _int("NETFLIX_PROFILE_WAIT", 15)     # cold launch -> profile picker visible
NETFLIX_HOME_WAIT = _int("NETFLIX_HOME_WAIT", 8)            # profile chosen -> Netflix home
NETFLIX_TITLE_WAIT = _int("NETFLIX_TITLE_WAIT", 6)          # deep link sent -> title page

PRIME_PICKER_WAIT = _int("PRIME_PICKER_WAIT", 15)           # cold launch -> profile picker visible
# A single ENTER 6s after the link did NOT pick the profile; ENTER typed by hand
# later did. 15 is the safer default; lower it once it works reliably.
PRIME_LINK_TO_KEY_WAIT = _int("PRIME_LINK_TO_KEY_WAIT", 15)
PRIME_TITLE_WAIT = _int("PRIME_TITLE_WAIT", 8)              # profile chosen -> title page

# Keys that pick the Prime profile. Verified: the first ENTER picks the profile.
PRIME_PROFILE_KEYS = _list("PRIME_PROFILE_KEYS", "ENTER")

# ---------------------------------------------------------------- Device server safety
# While True, device_mcp.py never touches the TV: play_title only reports what it
# WOULD do. Safe by default. Set DEVICE_DRY_RUN=0 in .env for real runs (Step 5).
DEVICE_DRY_RUN = _bool("DEVICE_DRY_RUN", True)