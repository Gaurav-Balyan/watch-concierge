"""
Prime Video add-on for the Household Watch Concierge.

This file only IMPORTS from phase0_spike.py (connect_with_wake, COUNTRY, ...).
It does not modify it, so your working Netflix/YouTube script is untouched.
Keep both files in the same folder and run from that folder.

Usage:
    python prime_video.py watch "The Boys"            # find on Prime India, open title page
    python prime_video.py watch "The Boys" 2019       # optional year to disambiguate
    python prime_video.py watch "The Boys" 2019 yes   # also press OK after the title opens
    python prime_video.py services "The Boys"         # every service + link in India
    python prime_video.py open amzn1.dv.gti.<uuid>    # open a title by Prime ID or full link
    python prime_video.py keys <gti> 6 LEFT,ENTER     # experiment: find the profile key sequence
    python prime_video.py probe <gti>                 # interactive: press keys one at a time

How Prime differs from Netflix (found by testing on this TV):
    Netflix : launch -> pick profile -> THEN send link
    Prime   : launch -> send link WHILE the profile picker shows -> pick profile
              (a link sent after the profile is chosen resets Prime to home)

Optional .env settings:
    PRIME_PROFILE_KEYS=ENTER        # keys that pick the profile
    PRIME_LINK_TO_KEY_WAIT=15       # seconds between sending the link and the profile key
"""
import asyncio
import os
import re
import sys

import httpx
from aiowebostv import WebOsClient

# Reused from the working script; importing it does NOT run its commands
# because its main() is guarded by `if __name__ == "__main__"`.
from phase0_spike import COUNTRY, connect_with_wake

# ---------------------------------------------------------------- Settings
PRIME_APP = "amazon"            # app ID on this TV (from `apps`)

PRIME_PICKER_WAIT = 15          # cold Prime launch -> profile picker visible
PRIME_LINK_TO_KEY_WAIT = int(os.environ.get("PRIME_LINK_TO_KEY_WAIT", "6"))  # after link, before profile key;
# a single ENTER at 6s did NOT pick the profile, but ENTER typed by hand ~10s+ later did: try 15
PRIME_TITLE_WAIT = 8            # after choosing a profile -> title page

# UNVERIFIED: plain ENTER did not pick the profile on this TV. Use the `keys`
# command to find a sequence that works, then set PRIME_PROFILE_KEYS in .env.
PRIME_PROFILE_KEYS = [k.strip() for k in
                      os.environ.get("PRIME_PROFILE_KEYS", "ENTER").split(",") if k.strip()]


# ---------------------------------------------------------------- Helpers

async def ask(prompt: str) -> str:
    """input() that doesn't freeze the event loop (keeps the TV connection alive)."""
    return await asyncio.to_thread(input, prompt)


async def send_link(c: WebOsClient, app: str, content_id: str, timeout: int = 20):
    """Send a content-id launch without crashing if the TV doesn't reply.

    Catching CancelledError is fine for a spike (the library cancels the request
    when the connection lapses); don't keep that in production code.
    """
    try:
        resp = await asyncio.wait_for(c.launch_app_with_content_id(app, content_id), timeout)
        print("TV response:", resp)
        return resp
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        print(f"No reply from TV ({type(e).__name__}). Check the screen anyway.")
        return None


async def press(c: WebOsClient, keys, gap: float = 1.5):
    """Press one or more remote keys with a short gap between them."""
    for key in ([keys] if isinstance(keys, str) else keys):
        print("  key:", key)
        await c.button(key)
        await asyncio.sleep(gap)


def to_gti(link_or_id: str) -> str:
    """Accept a full Prime link or a bare ID; return the bare amzn1.dv.gti.<uuid>."""
    return link_or_id.split("gti=")[-1] if "gti=" in link_or_id else link_or_id


# ---------------------------------------------------------------- Lookup

async def search_shows(title: str) -> list:
    """Streaming Availability search for India. Returns the raw show list."""
    rapid = os.environ.get("RAPIDAPI_KEY")
    if not rapid:
        sys.exit("Missing setting RAPIDAPI_KEY. Put it in .env or set it in your shell.")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(
            "https://streaming-availability.p.rapidapi.com/shows/search/title",
            params={"title": title, "country": COUNTRY},
            headers={
                "X-RapidAPI-Key": rapid,
                "X-RapidAPI-Host": "streaming-availability.p.rapidapi.com",
            },
        )
    r.raise_for_status()
    return r.json()


def show_year(show: dict) -> str:
    """Movies have releaseYear; series have firstAirYear. Use whichever exists."""
    return str(show.get("releaseYear") or show.get("firstAirYear") or "?")


async def find_prime(title: str, year: str | None = None):
    """Return [(title, year, gti)] for titles on Prime Video India.

    Subscription titles only, so the script never lands on a rent/buy screen.
    """
    found = []
    for show in await search_shows(title):
        for o in show.get("streamingOptions", {}).get(COUNTRY, []):
            if o.get("service", {}).get("id") != "prime" or o.get("type") != "subscription":
                continue
            m = re.search(r"gti=(amzn1\.dv\.gti\.[0-9a-fA-F-]+)", o.get("link", "") or "")
            if m:
                found.append((show.get("title"), show_year(show), m.group(1)))
                break
    if year:
        found = [f for f in found if f[1] == str(year)]
    return found


# ---------------------------------------------------------------- Prime flow

async def close_prime(c: WebOsClient):
    """Close Prime if it is open so the next launch starts at the profile picker."""
    if c.tv_state.current_app_id == PRIME_APP:
        try:
            await c.close_app(PRIME_APP)
        except Exception as e:  # noqa: BLE001
            print("  close_app failed, continuing:", type(e).__name__, e)
        await asyncio.sleep(3)


async def open_prime_title(c: WebOsClient, gti: str, press_play: bool = False):
    """Launch Prime, send the link while the profile picker shows, then pick a profile.

    UNVERIFIED: which keys pick the profile (see PRIME_PROFILE_KEYS).
    press_play is off by default: the focused button on Prime's title page may be
    a rent/buy button, and ENTER must never be sent blind there.
    """
    await close_prime(c)
    await c.launch_app(PRIME_APP)
    await asyncio.sleep(PRIME_PICKER_WAIT)       # profile picker visible

    await send_link(c, PRIME_APP, gti)
    await asyncio.sleep(PRIME_LINK_TO_KEY_WAIT)  # let Prime take the link
    await press(c, PRIME_PROFILE_KEYS)           # choose the profile
    await asyncio.sleep(PRIME_TITLE_WAIT)        # title page loads
    if press_play:
        await c.button("ENTER")
        await asyncio.sleep(3)
    print("Done. Check the TV: did the profile get picked and the title page open?")


# ---------------------------------------------------------------- Commands

async def cmd_services(title: str):
    """Show every streaming service and link for a title in India."""
    for show in (await search_shows(title))[:3]:
        print(f"- {show.get('title')} ({show_year(show)})")
        for o in show.get("streamingOptions", {}).get(COUNTRY, []):
            print(f"    {o.get('service', {}).get('id')}: {o.get('type')} {o.get('link')}")


async def cmd_watch(title: str, year: str | None = None, play: str = "no"):
    """Find a title on Prime India, wake the TV, and open it (play=yes presses OK)."""
    matches = await find_prime(title, year)
    if not matches:
        print(f"'{title}' is not on Prime Video India (subscription).")
        return
    if len(matches) > 1:
        print("Multiple matches, using the first. Pass a year to pick another:")
        for t, y, g in matches:
            print(f"  {t} ({y}) {g}")
    t, y, gti = matches[0]
    print(f"Opening: {t} ({y}) -> {gti}")

    c = await connect_with_wake()
    try:
        await open_prime_title(c, gti, press_play=(play == "yes"))
    finally:
        await c.disconnect()


async def cmd_open(gti: str, play: str = "no"):
    """Open a Prime title by ID or full link (skips search)."""
    c = await connect_with_wake()
    try:
        await open_prime_title(c, to_gti(gti), press_play=(play == "yes"))
    finally:
        await c.disconnect()


async def cmd_keys(gti: str, wait: str = "6", keys: str = "ENTER"):
    """Find the key sequence that picks the Prime profile.

    Launches Prime, sends the link at the profile picker, waits `wait` seconds,
    then presses `keys` (comma-separated). Try one at a time:
        keys <gti> 6 ENTER
        keys <gti> 6 LEFT,ENTER
        keys <gti> 10 RIGHT,LEFT,ENTER
    When one opens the title, set PRIME_PROFILE_KEYS in .env to that sequence.
    """
    c = await connect_with_wake()
    try:
        await close_prime(c)
        await c.launch_app(PRIME_APP)
        await asyncio.sleep(PRIME_PICKER_WAIT)

        await send_link(c, PRIME_APP, to_gti(gti))
        print(f"Waiting {wait}s, then pressing: {keys}")
        await asyncio.sleep(float(wait))
        await press(c, [k.strip() for k in keys.split(",")])

        await asyncio.sleep(8)
        print("current app:", c.tv_state.current_app_id)
        await ask("Did a profile get picked and the title page open? Note it, then press Enter...")
    finally:
        await c.disconnect()


async def cmd_probe(gti: str):
    """Interactive: send the link at the picker, then type keys one at a time.

    Type a key name (ENTER, LEFT, RIGHT, UP, DOWN, BACK) and watch the TV.
    Type q to quit. Use it to learn what the picker responds to, then put the
    working sequence in PRIME_PROFILE_KEYS.
    """
    c = await connect_with_wake()
    try:
        await close_prime(c)
        await c.launch_app(PRIME_APP)
        await asyncio.sleep(PRIME_PICKER_WAIT)
        await send_link(c, PRIME_APP, to_gti(gti))
        await asyncio.sleep(PRIME_LINK_TO_KEY_WAIT)
        sent = []
        while True:
            key = (await ask("Key to press (ENTER/LEFT/RIGHT/UP/DOWN/BACK, q to quit): ")).strip().upper()
            if key in ("Q", "QUIT", ""):
                break
            try:
                await c.button(key)
                sent.append(key)
            except Exception as e:  # noqa: BLE001
                print("  error:", type(e).__name__, e)
        print("Keys you pressed, in order:", ",".join(sent))
    finally:
        await c.disconnect()


# ---------------------------------------------------------------- Dispatcher

def main():
    """Run cmd_<name> for `python prime_video.py <name> [args...]`."""
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