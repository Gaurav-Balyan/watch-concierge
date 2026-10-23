"""
Netflix flow (found by testing on this TV).

Netflix only accepts a deep link when the app is already running with a
profile selected, so the order is:
    launch -> wait for profile picker -> ENTER (first profile) -> home screen
    -> send link (lands on the title page) -> ENTER to press Play.
"""
from aiowebostv import WebOsClient

import config
import tv

NAME = "netflix"
DEFAULT_PLAY = True     # the Play button is focused by default on the title page


def content_id(netflix_id: str) -> str:
    """The only deep-link format that resolved correctly on this TV."""
    return f"m=https://www.netflix.com/watch/{netflix_id}&source_type=4"


async def open_title(c: WebOsClient, netflix_id: str, press_play: bool = DEFAULT_PLAY,
                     profile: int | None = None):
    """Make sure Netflix is running with a profile selected, then deep link.

    profile=None: keep whatever profile is active (or ENTER on the highlighted one).
    profile=N   : restart Netflix so the picker shows, and pick the N-th profile (Phase 2).

    Note: "Netflix is the foreground app" does not prove a profile is chosen.
    """
    if profile is not None:
        # Get to the picker. close_app is UNRELIABLE on this TV (sometimes 403 access denied),
        # but relaunching a still-running Netflix was also seen to bring up the picker.
        await tv.close_app(c, config.NETFLIX_APP)
        await c.launch_app(config.NETFLIX_APP)
        await tv.pause("netflix picker", config.NETFLIX_PICKER_WAIT, ready=lambda: c.tv_state.current_app_id == config.NETFLIX_APP)
        await tv.press(c, tv.profile_keys(profile, config.NETFLIX_PICKER_LAYOUT), gap=0.8)
        await tv.pause("netflix home", config.NETFLIX_HOME_WAIT)
    elif c.tv_state.current_app_id != config.NETFLIX_APP:
        await c.launch_app(config.NETFLIX_APP)
        await tv.pause("netflix picker", config.NETFLIX_PROFILE_WAIT, ready=lambda: c.tv_state.current_app_id == config.NETFLIX_APP)
        await c.button("ENTER")                      # the highlighted profile
        await tv.pause("netflix home", config.NETFLIX_HOME_WAIT)

    await tv.send_link(c, config.NETFLIX_APP, content_id(netflix_id))
    await tv.pause("netflix title page", config.NETFLIX_TITLE_WAIT)
    if press_play:
        await c.button("ENTER")
        await tv.pause("after play", 3)