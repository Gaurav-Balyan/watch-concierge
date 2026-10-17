"""
One module per streaming service.

Every controllable service exposes the same small interface:
    NAME          : the service id used by lookup.py ("netflix", "prime")
    DEFAULT_PLAY  : whether `watch` presses Play after opening the title
    open_title(c, content_id, press_play) : open (and optionally play) a title

To add a service (JioHotstar, SonyLIV, ...): add a module with that interface,
teach lookup.extract_ids to find its ID, and register it below.
YouTube is separate because it is searched by name, not by catalogue ID.
"""
from . import netflix, prime, youtube

SERVICES = {
    netflix.NAME: netflix,
    prime.NAME: prime,
}

__all__ = ["SERVICES", "netflix", "prime", "youtube"]
