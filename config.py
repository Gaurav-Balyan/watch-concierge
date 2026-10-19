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

# Whether `watch`/play_title also presses Play on Prime's title page. Off by default:
# the focused button could be rent/buy on some titles. Play was verified to work on
# subscription titles, and the catalog only returns subscription titles, so turning it
# on is reasonable. It is a setting in code, never a parameter the model can choose.
PRIME_PRESS_PLAY = _bool("PRIME_PRESS_PLAY", False)

# Keys that pick the Prime profile. Verified: the first ENTER picks the profile.
PRIME_PROFILE_KEYS = _list("PRIME_PROFILE_KEYS", "ENTER")

# ---------------------------------------------------------------- Device server safety
# While True, device_mcp.py never touches the TV: play_title only reports what it
# WOULD do. Safe by default. Set DEVICE_DRY_RUN=0 in .env for real runs (Step 5).
DEVICE_DRY_RUN = _bool("DEVICE_DRY_RUN", True)

# ---------------------------------------------------------------- Identity (Phase 2)
# Who issues tokens. MCP servers accept only tokens signed by this issuer's keys.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "http://localhost:8080/realms/household")

# Where each MCP server listens when run with --http (Phase 2). 127.0.0.1 = this PC only.
CATALOG_MCP_PORT = _int("CATALOG_MCP_PORT", 8001)
# Set to http://127.0.0.1:8001/mcp to make the agent use catalog-mcp over HTTP (needs python login.py).
# Empty = start catalog_mcp.py over stdio as in Phase 1.
CATALOG_MCP_URL = os.environ.get("CATALOG_MCP_URL", "")
DEVICE_MCP_PORT = _int("DEVICE_MCP_PORT", 8002)
# Set to http://127.0.0.1:8002/mcp to make the agent use device-mcp over HTTP (needs python login.py).
DEVICE_MCP_URL = os.environ.get("DEVICE_MCP_URL", "")
TASTE_MCP_PORT = _int("TASTE_MCP_PORT", 8003)
# Set to http://127.0.0.1:8003/mcp to give the agent the taste tools (HTTP only: they need to know WHO asks).
TASTE_MCP_URL = os.environ.get("TASTE_MCP_URL", "")
# taste_mcp.py connects as the limited role taste_app (see db/01_init.sql), never as the superuser.
TASTE_DB_URL = os.environ.get("TASTE_DB_URL", "postgresql://taste_app:taste-dev-pass@127.0.0.1:5433/household")