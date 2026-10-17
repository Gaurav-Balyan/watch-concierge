"""
Netflix flow (found by testing on this TV).

Netflix only accepts a deep link when the app is already running with a
profile selected, so the order is:
    launch -> wait for profile picker -> ENTER (first profile) -> home screen
    -> send link (lands on the title page) -> ENTER to press Play.
"""
import asyncio

from aiowebostv import WebOsClient

import config
import tv

NAME = "netflix"
DEFAULT_PLAY = True     # the Play button is focused by default on the title page


def content_id(netflix_id: str) -> str:
    """The only deep-link format that resolved correctly on this TV."""
    return f"m=https://www.netflix.com/watch/{netflix_id}&source_type=4"


async def open_title(c: WebOsClient, netflix_id: str, press_play: bool = DEFAULT_PLAY):
    """Make sure Netflix is running with a profile selected, then deep link.

    Note: "Netflix is the foreground app" does not prove a profile is chosen.
    """
    if c.tv_state.current_app_id != config.NETFLIX_APP:
        await c.launch_app(config.NETFLIX_APP)
        await asyncio.sleep(config.NETFLIX_PROFILE_WAIT)
        await c.button("ENTER")                      # first profile
        await asyncio.sleep(config.NETFLIX_HOME_WAIT)

    await tv.send_link(c, config.NETFLIX_APP, content_id(netflix_id))
    await asyncio.sleep(config.NETFLIX_TITLE_WAIT)
    if press_play:
        await c.button("ENTER")
        await asyncio.sleep(3)
