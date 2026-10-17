"""
Everything that talks to the TV itself: connect, wake, press keys, send links.

Nothing in here knows about Netflix, Prime or YouTube.
"""
import asyncio
import warnings

from aiowebostv import WebOsClient
from wakeonlan import send_magic_packet

import config


async def connect() -> WebOsClient:
    """Open a connection to the TV using the saved pairing key (if any)."""
    ip = config.require("TV_IP")
    key = config.KEY_FILE.read_text().strip() if config.KEY_FILE.exists() else None
    client = WebOsClient(ip, client_key=key)
    await client.connect()  # first run: accept the prompt on the TV
    if client.client_key and client.client_key != key:
        config.KEY_FILE.write_text(client.client_key)
        print("Saved client key to", config.KEY_FILE)
    return client


def send_wake():
    """Send Wake-on-LAN magic packets: default broadcast + subnet broadcast.

    Harmless if the TV is already on. The warning filter hides the
    'Use wake() instead' DeprecationWarning from newer wakeonlan versions.
    """
    ip = config.require("TV_IP")
    mac = config.require("TV_MAC")
    bcast = ip.rsplit(".", 1)[0] + ".255"   # assumes a /24 network
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        send_magic_packet(mac)
        send_magic_packet(mac, ip_address=bcast)


async def connect_with_wake(max_wait: int = 90) -> WebOsClient:
    """Always wake first, then retry connecting until the TV accepts.

    Waking first matters: in standby some TVs still accept the network
    connection while the screen stays off, so waiting for a failed connect
    before sending the packet would never wake them.
    """
    for _ in range(3):
        send_wake()
        await asyncio.sleep(1)

    waited = 0
    while waited < max_wait:
        try:
            c = await asyncio.wait_for(connect(), 8)
            print("Connected. Power state:", await c.get_power_state())
            await asyncio.sleep(config.POST_WAKE_WAIT)   # let the screen come up
            return c
        except Exception as e:  # noqa: BLE001
            waited += 10
            print(f"  waiting for TV... ({waited}s) {type(e).__name__}")
            send_wake()
            await asyncio.sleep(2)
    raise RuntimeError("TV did not come up after wake")


async def ask(prompt: str) -> str:
    """input() that doesn't freeze the event loop (keeps the TV connection alive)."""
    return await asyncio.to_thread(input, prompt)


async def send_link(c: WebOsClient, app: str, content_id: str, timeout: int = 20):
    """Send a content-id launch without crashing if the TV doesn't reply.

    Catching CancelledError is fine for a spike (the library cancels the request
    when the connection lapses); don't keep that in production code.
    """
    try:
        resp = await asyncio.wait_for(c.launch_app_with_content_id(app, content_id), timeout)
        print("TV response:", resp)
        return resp
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        print(f"No reply from TV ({type(e).__name__}). Check the screen anyway.")
        return None


async def press(c: WebOsClient, keys, gap: float = 1.5):
    """Press one or more remote keys with a short gap between them."""
    for key in ([keys] if isinstance(keys, str) else keys):
        print("  key:", key)
        await c.button(key)
        await asyncio.sleep(gap)


async def close_app_if_open(c: WebOsClient, app: str):
    """Close an app if it is the foreground app, so the next launch starts fresh.

    UNVERIFIED on this TV: if close_app fails, we print a note and carry on.
    """
    if c.tv_state.current_app_id == app:
        try:
            await c.close_app(app)
        except Exception as e:  # noqa: BLE001
            print("  close_app failed, continuing:", type(e).__name__, e)
        await asyncio.sleep(3)
