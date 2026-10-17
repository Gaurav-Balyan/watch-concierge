"""
Phase 0 spike: Household Watch Concierge
Run on a machine on the same network as the LG TV.

Setup:
    pip install aiowebostv wakeonlan httpx
    export TV_IP=192.168.1.50
    export TV_MAC=AA:BB:CC:DD:EE:FF          # for wake-on-LAN
    export RAPIDAPI_KEY=...                  # Streaming Availability API
    export TMDB_KEY=...                      # TMDB v3 API key (only used by avail)

    (PowerShell: $env:TV_IP="192.168.1.50")

Usage:
    python phase0_spike.py pair                      # pair once, saves client key
    python phase0_spike.py info                      # TV model + webOS version
    python phase0_spike.py wake                      # wake-on-LAN test
    python phase0_spike.py netflix                   # launch Netflix app only
    python phase0_spike.py link 82182295             # open + play a Netflix title ID
    python phase0_spike.py yt dQw4w9WgXcQ            # YouTube deep link test
    python phase0_spike.py avail "Inception"         # availability (country IN) + TMDB lookup
    python phase0_spike.py watch "The Friend"        # END TO END: lookup, wake, launch, play
    python phase0_spike.py watch "The Friend" 2025   # optional year to disambiguate

How `watch` flows:
    main -> cmd_watch -> find_netflix (title -> Netflix ID)
                      -> connect_with_wake (send_wake + connect)
                      -> open_netflix_title (launch, pick profile, deep link, Play)
"""
import asyncio
import json
import os
import re
import sys
import warnings
from pathlib import Path

import httpx
from aiowebostv import WebOsClient
from wakeonlan import send_magic_packet
from dotenv import load_dotenv

load_dotenv()  # load .env if present, so the env vars are available to the script

# ---------------------------------------------------------------- Settings
# Defaults are kept so the spike runs as-is. Prefer env vars before sharing/committing.
TV_IP = os.environ["TV_IP"]
TV_MAC = os.environ["TV_MAC"]
KEY_FILE = Path(".webos_key")   # pairing key saved here so the TV doesn't ask every time
COUNTRY = "in"                  # country code for streaming availability lookups

# Tunable delays (seconds). If a step fires too early, raise these first.
PROFILE_PICKER_WAIT = 15   # after launching Netflix cold, wait for the profile picker
HOME_SCREEN_WAIT = 8       # after choosing a profile, wait for the Netflix home screen
TITLE_PAGE_WAIT = 6        # after sending the deep link, wait for the title page
POST_WAKE_WAIT = 5         # extra settle time once the TV answers after wake


# ---------------------------------------------------------------- TV connection

async def connect() -> WebOsClient:
    """Open a connection to the TV using the saved pairing key (if any)."""
    key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else None
    client = WebOsClient(TV_IP, client_key=key)
    await client.connect()  # first run: accept the prompt on the TV
    if client.client_key and client.client_key != key:
        KEY_FILE.write_text(client.client_key)
        print("Saved client key to", KEY_FILE)
    return client


def send_wake():
    """Send Wake-on-LAN magic packets: default broadcast + subnet broadcast.

    Harmless if the TV is already on. The warning filter hides the
    'Use wake() instead' DeprecationWarning from newer wakeonlan versions.
    """
    bcast = TV_IP.rsplit(".", 1)[0] + ".255"   # assumes a /24 network
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        send_magic_packet(TV_MAC)
        send_magic_packet(TV_MAC, ip_address=bcast)


async def connect_with_wake(max_wait: int = 90) -> WebOsClient:
    """Always wake first, then retry connecting until the TV accepts.

    Waking first matters: in standby some TVs still accept the network
    connection while the screen stays off, so waiting for a failed connect
    before sending the packet would never wake them.
    """
    for _ in range(3):
        send_wake()
        await asyncio.sleep(1)

    waited = 0
    while waited < max_wait:
        try:
            c = await asyncio.wait_for(connect(), 8)
            print("Connected. Power state:", await c.get_power_state())
            await asyncio.sleep(POST_WAKE_WAIT)   # let the screen come up
            return c
        except Exception as e:  # noqa: BLE001
            waited += 10
            print(f"  waiting for TV... ({waited}s) {type(e).__name__}")
            send_wake()
            await asyncio.sleep(2)
    raise RuntimeError("TV did not come up after wake")


# ---------------------------------------------------------------- Netflix helpers

def netflix_content_id(netflix_id: str) -> str:
    """The only deep-link format that resolved correctly on this TV.

    Only works when Netflix is already running with a profile selected.
    """
    return f"m=https://www.netflix.com/watch/{netflix_id}&source_type=4"


async def open_netflix_title(c: WebOsClient, netflix_id: str, press_play: bool = True):
    """Make sure Netflix is running with a profile selected, then deep link.

    Netflix ignores or stalls on deep links during a cold start, so:
    launch -> wait for profile picker -> ENTER (first profile) -> home screen
    -> send link (lands on the title page) -> ENTER to press Play.
    """
    if c.tv_state.current_app_id != "netflix":
        await c.launch_app("netflix")
        await asyncio.sleep(PROFILE_PICKER_WAIT)
        await c.button("ENTER")              # first profile
        await asyncio.sleep(HOME_SCREEN_WAIT)

    print(await c.launch_app_with_content_id("netflix", netflix_content_id(netflix_id)))
    await asyncio.sleep(TITLE_PAGE_WAIT)
    if press_play:
        await c.button("ENTER")              # Play button is focused by default
        await asyncio.sleep(3)


async def find_netflix(title: str, year: str | None = None):
    """Look up a title on Streaming Availability for India.

    Returns [(title, year, netflix_id)] for results available on Netflix,
    optionally filtered to one release year.
    """
    rapid = os.environ["RAPIDAPI_KEY"]
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(
            "https://streaming-availability.p.rapidapi.com/shows/search/title",
            params={"title": title, "country": COUNTRY},
            headers={
                "X-RapidAPI-Key": rapid,
                "X-RapidAPI-Host": "streaming-availability.p.rapidapi.com",
            },
        )
    r.raise_for_status()
    found = []
    for show in r.json():
        for o in show.get("streamingOptions", {}).get(COUNTRY, []):
            if o.get("service", {}).get("id") != "netflix":
                continue
            m = re.search(r"/title/(\d+)", o.get("link", ""))
            if m:
                found.append((show.get("title"), str(show.get("releaseYear")), m.group(1)))
                break
    if year:
        found = [f for f in found if f[1] == str(year)]
    return found


# ---------------------------------------------------------------- Commands

async def cmd_pair():
    """Connect once so the TV shows its approval prompt; saves the key."""
    c = await connect()
    print("Paired OK.")
    await c.disconnect()


async def cmd_info():
    """Print TV details and state (model, webOS version)."""
    c = await connect()
    print(json.dumps(
        c.tv_info.__dict__ if hasattr(c.tv_info, "__dict__") else str(c.tv_info),
        indent=2, default=str,
    ))
    print("System info:", c.tv_state if hasattr(c, "tv_state") else "n/a")
    await c.disconnect()


def cmd_wake():
    """Send wake packets only (no connection). Tests the TV's wake setup."""
    send_wake()
    print("Magic packets sent.")


async def cmd_netflix():
    """List the Netflix app entry and launch Netflix (no deep link)."""
    c = await connect()
    apps = await c.get_apps()
    nf = [a for a in apps if "netflix" in a["id"].lower() or "netflix" in a["title"].lower()]
    print("Netflix app entries:", nf)
    await c.launch_app("netflix")
    print("Launched Netflix.")
    await c.disconnect()


async def cmd_link(title_id: str):
    """Open and play one Netflix title by ID (skips search and wake)."""
    c = await connect()
    await open_netflix_title(c, title_id, press_play=True)
    await c.disconnect()


async def cmd_yt(video_id: str):
    """YouTube deep link test: proves the TV accepts launch-with-params."""
    c = await connect()
    params = {"contentTarget": f"https://www.youtube.com/watch?v={video_id}"}
    print(params)
    print(await c.launch_app_with_params("youtube.leanback.v4", params))
    await asyncio.sleep(8)
    print("current app:", c.tv_state.current_app_id)
    await c.disconnect()


async def cmd_avail(title: str):
    """Diagnostic: Netflix India availability, plus optional TMDB top matches."""
    rapid = os.environ["RAPIDAPI_KEY"]
    tmdb = os.environ["TMDB_KEY"]
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(
            "https://streaming-availability.p.rapidapi.com/shows/search/title",
            params={"title": title, "country": COUNTRY},
            headers={
                "X-RapidAPI-Key": rapid,
                "X-RapidAPI-Host": "streaming-availability.p.rapidapi.com",
            },
        )
        print("Streaming Availability status:", r.status_code)
        for show in r.json()[:5]:
            opts = show.get("streamingOptions", {}).get(COUNTRY, [])
            nf = [o for o in opts if o.get("service", {}).get("id") == "netflix"]
            print(f"- {show.get('title')} ({show.get('releaseYear')}) netflix_in={bool(nf)}")
            for o in nf:
                print("    link:", o.get("link"))

        # TMDB is optional metadata; a network hiccup must not kill the command.
        if not tmdb:
            print("\nTMDB_KEY not set, skipping TMDB lookup.")
            return
        try:
            t = await http.get(
                "https://api.themoviedb.org/3/search/movie",
                params={"query": title, "api_key": tmdb},
            )
            print("\nTMDB top results:")
            for m in t.json().get("results", [])[:3]:
                print(f"- {m['title']} ({m.get('release_date', '')[:4]}) id={m['id']}")
        except httpx.HTTPError as e:
            print("\nTMDB lookup skipped:", type(e).__name__, e)


async def cmd_watch(title: str, year: str | None = None):
    """End to end: find on Netflix India, wake TV, launch Netflix, deep link, play."""
    matches = await find_netflix(title, year)
    if not matches:
        print(f"'{title}' is not on Netflix India. (Try another service.)")
        return
    if len(matches) > 1:
        print("Multiple matches, using the first. Pass a year to pick another:")
        for t, y, nid in matches:
            print(f"  {t} ({y}) id={nid}")
    t, y, nid = matches[0]
    print(f"Playing: {t} ({y}) -> Netflix id {nid}")

    c = await connect_with_wake()
    await open_netflix_title(c, nid, press_play=True)
    await c.disconnect()
    
def find_youtube(query: str):
    """Search YouTube and return (video_id, title) for the top result."""
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = info.get("entries") or []
    if not entries:
        return None
    return entries[0]["id"], entries[0].get("title")


async def cmd_song(*words):
    """Search YouTube for a song by name, wake the TV, and play the top result."""
    query = " ".join(words)
    result = await asyncio.to_thread(find_youtube, query)
    if not result:
        print(f"No YouTube result for '{query}'.")
        return
    video_id, vtitle = result
    print(f"Playing: {vtitle} ({video_id})")

    c = await connect_with_wake()
    params = {"contentTarget": f"https://www.youtube.com/watch?v={video_id}"}
    print(await c.launch_app_with_params("youtube.leanback.v4", params))
    await asyncio.sleep(8)
    print("current app:", c.tv_state.current_app_id)
    await c.disconnect()


# ---------------------------------------------------------------- Dispatcher

def main():
    """Run cmd_<name> for `python phase0_spike.py <name> [args...]`."""
    if len(sys.argv) < 2:
        print(__doc__)
        return
    name, args = sys.argv[1], sys.argv[2:]
    fn = globals().get(f"cmd_{name}")
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