"""
YouTube flow. The deep link works any time, with no profile step.

Unlike Netflix and Prime, YouTube is searched by name (via yt-dlp), not by a
streaming-catalogue ID, so it is not part of SERVICES.
"""
import asyncio

from aiowebostv import WebOsClient

import config


def find_video(query: str):
    """Search YouTube and return (video_id, title) for the top result, or None.

    Blocking (yt-dlp), so call it with asyncio.to_thread from async code.
    If it breaks after a YouTube change, run: pip install -U yt-dlp
    """
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    entries = info.get("entries") or []
    if not entries:
        return None
    return entries[0]["id"], entries[0].get("title")


async def play(c: WebOsClient, video_id: str):
    params = {"contentTarget": f"https://www.youtube.com/watch?v={video_id}"}
    print(await c.launch_app_with_params(config.YOUTUBE_APP, params))
    await asyncio.sleep(8)
    print("current app:", c.tv_state.current_app_id)
