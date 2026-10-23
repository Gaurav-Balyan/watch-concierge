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
# Used only when the person is unknown (stdio, or not in PRIME_PROFILES below).
PRIME_PROFILE_KEYS = _list("PRIME_PROFILE_KEYS", "ENTER")


# ---------------------------------------------------------------- Per-person profiles (Phase 2, Step 9)
def _profiles(name: str, default: str) -> dict[str, int]:
    """'gaurav:3,mishika:4' -> {'gaurav': 3, 'mishika': 4}. Positions count from 1, left to right."""
    out = {}
    for pair in _list(name, default):
        user, _, pos = pair.partition(":")
        if not pos.strip().isdigit() or int(pos) < 1:
            sys.exit(f"Setting {name}: expected user:position pairs like gaurav:3, got {pair!r}.")
        out[user.strip().lower()] = int(pos)
    return out


# Position of each person's profile on each app's picker (logged-in username -> position).
NETFLIX_PROFILES = _profiles("NETFLIX_PROFILES", "gaurav:3,mishika:4")
PRIME_PROFILES = _profiles("PRIME_PROFILES", "gaurav:1,mishika:3")
# How each app's picker is laid out. Verified on this TV (2026-10-20): BOTH Netflix and Prime stack
# profiles in a COLUMN (UP/DOWN move between them). On Netflix, LEFT/RIGHT reach the edit pencil,
# so never press them on a picker.
NETFLIX_PICKER_LAYOUT = os.environ.get("NETFLIX_PICKER_LAYOUT", "column")
PRIME_PICKER_LAYOUT = os.environ.get("PRIME_PICKER_LAYOUT", "column")
# The picker highlights the LAST USED profile, so we first move to the first profile
# (UP or LEFT this many times), then DOWN/RIGHT (position - 1) times, then ENTER.
PROFILE_RESET_PRESSES = _int("PROFILE_RESET_PRESSES", 5)
# Measured: after a real close, Netflix took 22 s to show the picker (15 s was too short).
NETFLIX_PICKER_WAIT = _int("NETFLIX_PICKER_WAIT", 25)

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

# --- Phase 3, Step 4: OpenTelemetry tracing (see tracing.py) ---
# TRACING: empty = off (default). "print" = span tree printed after each run. "jaeger" = send to Jaeger.
# "print,jaeger" = both.
TRACING = _list("TRACING", "")
# Where Jaeger (or any OpenTelemetry collector) listens for OTLP over HTTP.
OTLP_ENDPOINT = os.environ.get("OTLP_ENDPOINT", "http://localhost:4318")
# 1 = also put tool RESULTS in spans (they can hold personal data, e.g. watch history). Args are always kept.
TRACE_CONTENT = _bool("TRACE_CONTENT", False)
# Phase 3, Step 7: a SMALL, fast Groq model for the router, used only when its rules are unsure.
# Empty = no model: unsure requests go straight to the full team (FALLBACK). See: python agent.py --models
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "")
# ---------------------------------------------------------------- Phase 4: resilience
# Small SQLite files that must outlive one process: graph checkpoints, the TV's idempotency records,
# the catalog cache and circuit breaker. In quick mode every request starts fresh stdio servers, so
# memory alone would forget them. Git-ignored. Tests and evals point it somewhere else.
STATE_DIR = Path(os.environ.get("STATE_DIR", str(BASE_DIR / ".state")))
# Catalog cache: a fresh answer is reused for this long (availability changes slowly; saves RapidAPI quota).
CATALOG_CACHE_TTL = _int("CATALOG_CACHE_TTL", 24 * 3600)
# When the catalog API is DOWN, an older cached answer (up to this age) is better than none.
CATALOG_STALE_MAX = _int("CATALOG_STALE_MAX", 7 * 24 * 3600)
# Retries for ONE catalog search: attempts, and the first backoff in seconds (doubled each time, with jitter).
CATALOG_RETRIES = _int("CATALOG_RETRIES", 3)
CATALOG_BACKOFF = float(os.environ.get("CATALOG_BACKOFF", "0.5"))
# Circuit breaker: after this many failed searches in a row, stop calling the API for CATALOG_BREAKER_RESET s.
CATALOG_BREAKER_FAILURES = _int("CATALOG_BREAKER_FAILURES", 3)
CATALOG_BREAKER_RESET = _int("CATALOG_BREAKER_RESET", 60)
# Groq: the SDK itself retries rate limits / 5xx / dropped connections with backoff. Retry at ONE layer only.
GROQ_MAX_RETRIES = _int("GROQ_MAX_RETRIES", 2)
# Fast TV check: after Wake-on-LAN, how long to wait for the TV to answer on the network at all.
# From standby it answers within seconds; from fully OFF it never will (WoL cannot wake it), so waiting
# the full connect loop (~40-90 s) was wasted. Raise this if a TV in standby is reported unreachable.
TV_WAKE_WINDOW = _int("TV_WAKE_WINDOW", 25)
# Failure drills ONLY (never set this in .env for normal use): crash the process at a graph node.
# "suggest" = crash as the node starts; "play:after" = crash after the node's work, before it is saved.
CHAOS_CRASH = os.environ.get("CHAOS_CRASH", "")