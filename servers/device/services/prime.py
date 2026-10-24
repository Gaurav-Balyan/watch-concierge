"""
Prime Video flow (found by testing on this TV). It is the REVERSE of Netflix:

    launch -> send link WHILE the profile picker is showing -> pick profile

Prime holds the link and opens the title once a profile is chosen. A link sent
after the profile is already chosen makes Prime reset to its home screen, so we
always start from a closed app.

Verified: the first ENTER picks the profile, and a second ENTER on the title
page presses Play (for subscription titles).
"""
from aiowebostv import WebOsClient

from common import config
from servers.device import tv

NAME = "prime"
# Off by default: on a title page the focused button could be a rent/buy button,
# so ENTER is never sent blind. Turn on with PRIME_PRESS_PLAY=1 in .env.
DEFAULT_PLAY = config.PRIME_PRESS_PLAY


def to_gti(link_or_id: str) -> str:
    """Accept a full Prime link or a bare ID; return the bare amzn1.dv.gti.<uuid>."""
    return link_or_id.split("gti=")[-1] if "gti=" in link_or_id else link_or_id


async def start_at_picker(c: WebOsClient):
    """Close Prime if open, relaunch it, and wait until the profile picker shows."""
    await tv.close_app_if_open(c, config.PRIME_APP)
    await c.launch_app(config.PRIME_APP)
    await tv.pause("prime picker", config.PRIME_PICKER_WAIT, ready=lambda: c.tv_state.current_app_id == config.PRIME_APP)


async def open_title(c: WebOsClient, gti: str, press_play: bool = DEFAULT_PLAY,
                     profile: int | None = None):
    """Launch Prime, send the link at the profile picker, pick the profile.

    profile=N picks the N-th profile (Phase 2); None keeps PRIME_PROFILE_KEYS (the highlighted one).
    """
    await start_at_picker(c)
    await tv.send_link(c, config.PRIME_APP, to_gti(gti))
    await tv.pause("prime link to key", config.PRIME_LINK_TO_KEY_WAIT)   # let the picker accept input
    if profile is not None:
        await tv.press(c, tv.profile_keys(profile, config.PRIME_PICKER_LAYOUT), gap=0.8)
    else:
        await tv.press(c, config.PRIME_PROFILE_KEYS)     # choose the highlighted profile
    await tv.pause("prime title page", config.PRIME_TITLE_WAIT)         # title page loads
    if press_play:
        await c.button("ENTER")
        await tv.pause("after play", 3)
    print("Done. Check the TV: did the profile get picked and the title page open?")