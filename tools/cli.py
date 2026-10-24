"""
Household Watch Concierge: SETUP and TUNING commands for the TV. Run from this folder.

Everyday use is NOT here: use  python -m concierge.graph "play The Friend 2025"  (login, per-person profiles,
dry run, idempotency, tracing). The direct-control commands below talk to the TV straight away, with
none of that: they exist to pair the TV and to tune the service flows (waits, key presses).

Setup and direct control:
    python -m tools.cli pair                          # pair once, saves client key
    python -m tools.cli info                          # TV model + webOS version
    python -m tools.cli wake                          # wake-on-LAN test
    python -m tools.cli netflix                       # launch Netflix only
    python -m tools.cli link 82182295                 # open + play a Netflix title ID
    python -m tools.cli open <gti or Prime link>      # open a Prime title (add `yes` to play)
    python -m tools.cli yt dQw4w9WgXcQ                # YouTube by video ID

Diagnostics and tuning (experiments.py):
    python -m tools.cli apps                          # list app IDs on the TV
    python -m tools.cli services "The Boys"           # every service + link in India
    python -m tools.cli avail "Inception"             # Netflix availability + TMDB lookup
    python -m tools.cli keys <gti> 15 ENTER           # test a Prime profile key sequence
    python -m tools.cli probe <gti>                   # press Prime picker keys one at a time
"""
import asyncio
import json
import sys

from common import config
from tools import experiments
from servers.device import tv
from servers.device.services import netflix, prime, youtube


def parse_year_play(args):
    """Pick an optional 4-digit year and an optional yes/no out of extra arguments."""
    year, play = None, None
    for a in args:
        low = a.lower()
        if low in ("yes", "play"):
            play = True
        elif low in ("no", "noplay"):
            play = False
        elif a.isdigit() and len(a) == 4:
            year = a
    return year, play


# ---------------------------------------------------------------- Setup and direct control

async def cmd_pair():
    """Connect once so the TV shows its approval prompt; saves the key."""
    c = await tv.connect()
    print("Paired OK.")
    await c.disconnect()


async def cmd_info():
    """Print TV details and state (model, webOS version)."""
    c = await tv.connect()
    try:
        print(json.dumps(
            c.tv_info.__dict__ if hasattr(c.tv_info, "__dict__") else str(c.tv_info),
            indent=2, default=str,
        ))
        print("System info:", c.tv_state if hasattr(c, "tv_state") else "n/a")
    finally:
        await c.disconnect()


def cmd_wake():
    """Send wake packets only (no connection). Tests the TV's wake setup."""
    tv.send_wake()
    print("Magic packets sent.")


async def cmd_netflix():
    """Launch Netflix (no deep link)."""
    c = await tv.connect()
    try:
        await c.launch_app(config.NETFLIX_APP)
        print("Launched Netflix.")
    finally:
        await c.disconnect()


async def cmd_link(netflix_id: str):
    """Open and play one Netflix title by ID (skips search and wake)."""
    c = await tv.connect()
    try:
        await netflix.open_title(c, netflix_id, press_play=True)
    finally:
        await c.disconnect()


async def cmd_open(gti: str, *rest):
    """Open a Prime title by ID or full link (skips search)."""
    _, play = parse_year_play(rest)
    c = await tv.connect_with_wake()
    try:
        await prime.open_title(c, gti, press_play=bool(play))
    finally:
        await c.disconnect()


async def cmd_yt(video_id: str):
    """YouTube deep link by video ID."""
    c = await tv.connect_with_wake()
    try:
        await youtube.play(c, video_id)
    finally:
        await c.disconnect()


# ---------------------------------------------------------------- Dispatcher

def build_commands() -> dict:
    """Collect every cmd_<name> from this file and experiments.py."""
    commands = {}
    for scope in (vars(experiments), globals()):
        for name, fn in scope.items():
            if name.startswith("cmd_") and callable(fn):
                commands[name[4:]] = fn
    return commands


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    name, args = sys.argv[1], sys.argv[2:]
    fn = build_commands().get(name)
    if fn is None:
        print(f"Unknown command: {name}\n")
        print(__doc__)
        return
    try:
        result = fn(*args)
    except TypeError as e:
        print(f"Bad arguments for '{name}': {e}\n")
        print(__doc__)
        return
    if asyncio.iscoroutine(result):
        asyncio.run(result)


if __name__ == "__main__":
    main()
