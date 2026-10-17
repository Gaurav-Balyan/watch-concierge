"""
Diagnostics and tuning commands you run rarely. Kept out of the everyday path.

All of these are exposed through cli.py, e.g.  python cli.py probe <gti>
"""
import asyncio
import os

import httpx

import config
import lookup
import tv
from services import prime


async def cmd_apps():
    """List every app ID and name installed on the TV."""
    c = await tv.connect()
    try:
        for a in await c.get_apps():
            print(a["id"], "-", a["title"])
    finally:
        await c.disconnect()


async def cmd_services(title: str):
    """Show every streaming service and link for a title in India."""
    for show in (await lookup.search_shows(title))[:3]:
        print(f"- {show.get('title')} ({lookup.show_year(show)})")
        for o in show.get("streamingOptions", {}).get(config.COUNTRY, []):
            print(f"    {o.get('service', {}).get('id')}: {o.get('type')} {o.get('link')}")


async def cmd_avail(title: str):
    """Netflix availability in India, plus optional TMDB top matches."""
    for show in (await lookup.search_shows(title))[:5]:
        opts = show.get("streamingOptions", {}).get(config.COUNTRY, [])
        nf = [o for o in opts if o.get("service", {}).get("id") == "netflix"]
        print(f"- {show.get('title')} ({lookup.show_year(show)}) netflix_in={bool(nf)}")
        for o in nf:
            print("    link:", o.get("link"))

    # TMDB is optional metadata; a network hiccup must not kill the command.
    tmdb = os.environ.get("TMDB_KEY")
    if not tmdb:
        print("\nTMDB_KEY not set, skipping TMDB lookup.")
        return
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            t = await http.get(
                "https://api.themoviedb.org/3/search/movie",
                params={"query": title, "api_key": tmdb},
            )
        print("\nTMDB top results:")
        for m in t.json().get("results", [])[:3]:
            print(f"- {m['title']} ({m.get('release_date', '')[:4]}) id={m['id']}")
    except httpx.HTTPError as e:
        print("\nTMDB lookup skipped:", type(e).__name__, e)


async def cmd_keys(gti: str, wait: str = "15", keys: str = "ENTER"):
    """Test a Prime profile key sequence.

    Launches Prime, sends the link at the profile picker, waits `wait` seconds,
    then presses `keys` (comma-separated). Examples:
        keys <gti> 15 ENTER
        keys <gti> 6 ENTER,ENTER
    When one opens the title, put it in .env as PRIME_PROFILE_KEYS.
    """
    c = await tv.connect_with_wake()
    try:
        await prime.start_at_picker(c)
        await tv.send_link(c, config.PRIME_APP, prime.to_gti(gti))
        print(f"Waiting {wait}s, then pressing: {keys}")
        await asyncio.sleep(float(wait))
        await tv.press(c, [k.strip() for k in keys.split(",")])
        await asyncio.sleep(8)
        print("current app:", c.tv_state.current_app_id)
        await tv.ask("Did a profile get picked and the title page open? Note it, then press Enter...")
    finally:
        await c.disconnect()


async def cmd_probe(gti: str):
    """Interactive: send the Prime link at the picker, then type keys one at a time.

    Type a key name (ENTER, LEFT, RIGHT, UP, DOWN, BACK) and watch the TV. Type q to quit.
    """
    c = await tv.connect_with_wake()
    try:
        await prime.start_at_picker(c)
        await tv.send_link(c, config.PRIME_APP, prime.to_gti(gti))
        await asyncio.sleep(config.PRIME_LINK_TO_KEY_WAIT)
        sent = []
        while True:
            key = (await tv.ask("Key to press (ENTER/LEFT/RIGHT/UP/DOWN/BACK, q to quit): ")).strip().upper()
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
