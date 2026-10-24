"""
YouTube PLAY flow for device-mcp. The deep link works any time, with no profile step.
(Searching YouTube is catalog-mcp's job: servers/catalog/youtube.py.)
"""
from aiowebostv import WebOsClient

from common import config
from servers.device import tv

# Phase 3: the same interface as netflix.py / prime.py, so device-mcp can play YouTube too.
NAME = "youtube"
DEFAULT_PLAY = True      # a YouTube deep link starts playing on its own


async def open_title(c: WebOsClient, content_id: str, press_play: bool = True, **_ignored):
    """device-mcp's interface. YouTube has no profile picker, so `profile` is ignored."""
    await play(c, content_id)


async def play(c: WebOsClient, video_id: str):
    params = {"contentTarget": f"https://www.youtube.com/watch?v={video_id}"}
    print(await c.launch_app_with_params(config.YOUTUBE_APP, params))
    await tv.pause("youtube start", 8, ready=lambda: c.tv_state.current_app_id == config.YOUTUBE_APP)
    print("current app:", c.tv_state.current_app_id)