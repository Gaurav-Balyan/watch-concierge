"""
Phase 0 spike: Household Watch Concierge
Run on a machine on the same network as the LG TV.

Setup:
    pip install aiowebostv wakeonlan httpx
    export TV_IP=192.168.1.50
    export TV_MAC=AA:BB:CC:DD:EE:FF          # for wake-on-LAN
    export RAPIDAPI_KEY=...                  # Streaming Availability API
    export TMDB_KEY=...                      # TMDB v3 API key

Usage:
    python phase0_spike.py pair              # pair once, saves client key
    python phase0_spike.py info              # TV model + webOS version
    python phase0_spike.py wake              # wake-on-LAN test
    python phase0_spike.py netflix           # launch Netflix app only
    python phase0_spike.py link 80057281     # try deep-link formats for a Netflix title ID
    python phase0_spike.py avail "Inception" # availability (country IN) + TMDB lookup
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from aiowebostv import WebOsClient
from wakeonlan import send_magic_packet

TV_IP = os.environ.get("TV_IP", "192.168.1.6")
TV_MAC = os.environ.get("TV_MAC", "70:97:41:9D:26:7C")
KEY_FILE = Path(".webos_key")
COUNTRY = "in"


async def connect() -> WebOsClient:
    key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else None
    client = WebOsClient(TV_IP, client_key=key)
    await client.connect()  # first run: accept the prompt on the TV
    if client.client_key and client.client_key != key:
        KEY_FILE.write_text(client.client_key)
        print("Saved client key to", KEY_FILE)
    return client


async def cmd_pair():
    c = await connect()
    print("Paired OK.")
    await c.disconnect()


async def cmd_info():
    c = await connect()
    print(json.dumps(c.tv_info.__dict__ if hasattr(c.tv_info, "__dict__") else str(c.tv_info), indent=2, default=str))
    print("System info:", c.tv_state if hasattr(c, "tv_state") else "n/a")
    await c.disconnect()


def cmd_wake():
    send_magic_packet(TV_MAC)
    print("Magic packet sent. Does the TV wake? Try from Wi-Fi AND Ethernet, standby for >30 min.")


async def cmd_netflix():
    c = await connect()
    apps = await c.get_apps()
    nf = [a for a in apps if "netflix" in a["id"].lower() or "netflix" in a["title"].lower()]
    print("Netflix app entries:", nf)
    await c.launch_app("netflix")
    print("Launched Netflix.")
    await c.disconnect()
   
async def cmd_link(title_id: str):
    c = await connect()
    content = f"m=https://www.netflix.com/watch/{title_id}&source_type=4"

    await c.launch_app("netflix")
    await asyncio.sleep(15)      # profile picker
    await c.button("ENTER")      # first profile
    await asyncio.sleep(8)       # home screen

    print(await c.launch_app_with_content_id("netflix", content))
    await asyncio.sleep(6)       # let the title page load and focus settle
    await c.button("ENTER")      # press Play
    await asyncio.sleep(3)
    await c.disconnect()

async def cmd_avail(title: str):
    rapid = os.environ["RAPIDAPI_KEY"]
    tmdb = os.environ["TMDB_KEY"]
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(
            "https://streaming-availability.p.rapidapi.com/shows/search/title",
            params={"title": title, "country": COUNTRY},
            headers={"X-RapidAPI-Key": rapid, "X-RapidAPI-Host": "streaming-availability.p.rapidapi.com"},
        )
        print("Streaming Availability status:", r.status_code)
        for show in r.json()[:5]:
            opts = show.get("streamingOptions", {}).get(COUNTRY, [])
            nf = [o for o in opts if o.get("service", {}).get("id") == "netflix"]
            print(f"- {show.get('title')} ({show.get('releaseYear')}) netflix_in={bool(nf)}")
            for o in nf:
                print("    link:", o.get("link"))

        t = await http.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"query": title, "api_key": tmdb},
        )
        print("\nTMDB top results:")
        for m in t.json().get("results", [])[:3]:
            print(f"- {m['title']} ({m.get('release_date', '')[:4]}) id={m['id']}")
            
async def cmd_yt(video_id: str):
    c = await connect()
    params = {"contentTarget": f"https://www.youtube.com/watch?v={video_id}"}
    print(params)
    print(await c.launch_app_with_params("youtube.leanback.v4", params))
    await asyncio.sleep(8)
    print("current app:", c.tv_state.current_app_id)
    await c.disconnect()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "pair":
        asyncio.run(cmd_pair())
    elif cmd == "info":
        asyncio.run(cmd_info())
    elif cmd == "netflix":
        asyncio.run(cmd_netflix())
    elif cmd == "wake":
        cmd_wake()                                   # only if you have this defined
    elif cmd == "link":
        asyncio.run(cmd_link(args[0]))
    elif cmd == "yt":
        asyncio.run(cmd_yt(args[0]))
    elif cmd == "avail":
        asyncio.run(cmd_avail(args[0]))              # only if you have this defined
    else:
        print(__doc__)


if __name__ == "__main__":
    main()