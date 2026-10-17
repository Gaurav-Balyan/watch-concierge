"""
The concierge: take a request, decide how to fulfil it, drive the TV.

    watch("The Boys")   -> look up the title, pick a service, wake the TV, open it
    song("tum hi ho")   -> find a video on YouTube, wake the TV, play it

This is the layer a chat, voice or WhatsApp front end would call later.
"""
import asyncio

import lookup
import tv
from services import SERVICES, youtube


async def watch(title: str, year: str | None = None, play: bool | None = None, only: str | None = None) -> bool:
    """Find a title on Netflix or Prime (India) and open it on the TV.

    year : optional release year to disambiguate
    play : True/False to force pressing Play; None uses each service's default
    only : restrict to one service ("netflix" or "prime")
    Returns True if something was opened.
    """
    matches = await lookup.find_title(title, year, only)
    if not matches:
        where = only.capitalize() if only else "Netflix or Prime (subscription)"
        print(f"'{title}' is not on {where} in India.")
        return False

    if len(matches) > 1:
        print("Multiple matches, using the first. Pass a year to pick another:")
        for m in matches:
            print(f"  {m.title} ({m.year}) on {', '.join(m.ids)}")

    match = matches[0]
    service_name = lookup.pick_service(match.ids, only)
    service = SERVICES[service_name]
    print(f"Opening: {match.title} ({match.year}) on {service_name}")

    c = await tv.connect_with_wake()
    try:
        await service.open_title(
            c,
            match.ids[service_name],
            press_play=service.DEFAULT_PLAY if play is None else play,
        )
    finally:
        await c.disconnect()
    return True


async def song(query: str) -> bool:
    """Search YouTube for a song by name and play the top result."""
    result = await asyncio.to_thread(youtube.find_video, query)
    if not result:
        print(f"No YouTube result for '{query}'.")
        return False
    video_id, video_title = result
    print(f"Playing: {video_title} ({video_id})")

    c = await tv.connect_with_wake()
    try:
        await youtube.play(c, video_id)
    finally:
        await c.disconnect()
    return True
