"""
Household Watch Concierge: command line.  Run from this folder:

Everyday:
    python cli.py watch "The Friend"            # Netflix or Prime, whichever has it
    python cli.py watch "The Boys" 2019         # optional year to disambiguate
    python cli.py watch "Game Changer" yes      # force pressing Play (Prime defaults to no)
    python cli.py watchnetflix "The Friend"     # Netflix only
    python cli.py watchprime "The Boys"         # Prime only
    python cli.py song tum hi ho arijit singh   # play a song on YouTube

Setup and direct control:
    python cli.py pair                          # pair once, saves client key
    python cli.py info                          # TV model + webOS version
    python cli.py wake                          # wake-on-LAN test
    python cli.py netflix                       # launch Netflix only
    python cli.py link 82182295                 # open + play a Netflix title ID
    python cli.py open <gti or Prime link>      # open a Prime title (add `yes` to play)
    python cli.py yt dQw4w9WgXcQ                # YouTube by video ID

Diagnostics and tuning (experiments.py):
    python cli.py apps                          # list app IDs on the TV
    python cli.py services "The Boys"           # every service + link in India
    python cli.py avail "Inception"             # Netflix availability + TMDB lookup
    python cli.py keys <gti> 15 ENTER           # test a Prime profile key sequence
    python cli.py probe <gti>                   # press Prime picker keys one at a time
"""
import asyncio
import json
import sys

import concierge
import config
import experiments
import tv
from services import netflix, prime, youtube


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


# ---------------------------------------------------------------- Everyday

async def cmd_watch(title: str, *rest):
    year, play = parse_year_play(rest)
    await concierge.watch(title, year, play)


async def cmd_watchnetflix(title: str, *rest):
    year, play = parse_year_play(rest)
    await concierge.watch(title, year, play, only="netflix")


async def cmd_watchprime(title: str, *rest):
    year, play = parse_year_play(rest)
    await concierge.watch(title, year, play, only="prime")


async def cmd_song(*words):
    await concierge.song(" ".join(words))


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
