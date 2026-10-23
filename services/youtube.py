"""
YouTube flow. The deep link works any time, with no profile step.

Unlike Netflix and Prime, YouTube is searched by name (via yt-dlp), not by a
streaming-catalogue ID, so it is not part of SERVICES.
"""
from aiowebostv import WebOsClient

import config
import tv

# Phase 3: the same interface as netflix.py / prime.py, so device-mcp can play YouTube too.
NAME = "youtube"
DEFAULT_PLAY = True      # a YouTube deep link starts playing on its own


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


def find_videos(query: str, limit: int = 3) -> list[dict]:
    """Top `limit` YouTube results as dicts: video_id, title, channel, duration_seconds.
    Blocking (yt-dlp): call it with asyncio.to_thread."""
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [{"video_id": e["id"], "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
             "duration_seconds": e.get("duration")} for e in (info.get("entries") or []) if e.get("id")]


async def open_title(c: WebOsClient, content_id: str, press_play: bool = True, **_ignored):
    """device-mcp's interface. YouTube has no profile picker, so `profile` is ignored."""
    await play(c, content_id)


async def play(c: WebOsClient, video_id: str):
    params = {"contentTarget": f"https://www.youtube.com/watch?v={video_id}"}
    print(await c.launch_app_with_params(config.YOUTUBE_APP, params))
    await tv.pause("youtube start", 8, ready=lambda: c.tv_state.current_app_id == config.YOUTUBE_APP)
    print("current app:", c.tv_state.current_app_id)