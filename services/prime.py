"""
Prime Video flow (found by testing on this TV). It is the REVERSE of Netflix:

    launch -> send link WHILE the profile picker is showing -> pick profile

Prime holds the link and opens the title once a profile is chosen. A link sent
after the profile is already chosen makes Prime reset to its home screen, so we
always start from a closed app.

Verified: the first ENTER picks the profile, and a second ENTER on the title
page presses Play (for subscription titles).
"""
import asyncio

from aiowebostv import WebOsClient

import config
import tv

NAME = "prime"
# Off by default: on a title page the focused button could be a rent/buy button,
# so ENTER is never sent blind. Pass play=yes on the command line to opt in.
DEFAULT_PLAY = False


def to_gti(link_or_id: str) -> str:
    """Accept a full Prime link or a bare ID; return the bare amzn1.dv.gti.<uuid>."""
    return link_or_id.split("gti=")[-1] if "gti=" in link_or_id else link_or_id


async def start_at_picker(c: WebOsClient):
    """Close Prime if open, relaunch it, and wait until the profile picker shows."""
    await tv.close_app_if_open(c, config.PRIME_APP)
    await c.launch_app(config.PRIME_APP)
    await asyncio.sleep(config.PRIME_PICKER_WAIT)


async def open_title(c: WebOsClient, gti: str, press_play: bool = DEFAULT_PLAY):
    """Launch Prime, send the link at the profile picker, pick the profile."""
    await start_at_picker(c)
    await tv.send_link(c, config.PRIME_APP, to_gti(gti))
    await asyncio.sleep(config.PRIME_LINK_TO_KEY_WAIT)   # let the picker accept input
    await tv.press(c, config.PRIME_PROFILE_KEYS)         # choose the profile
    await asyncio.sleep(config.PRIME_TITLE_WAIT)         # title page loads
    if press_play:
        await c.button("ENTER")
        await asyncio.sleep(3)
    print("Done. Check the TV: did the profile get picked and the title page open?")
