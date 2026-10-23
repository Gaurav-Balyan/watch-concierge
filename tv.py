"""
Everything that talks to the TV itself: connect, wake, press keys, send links.

Nothing in here knows about Netflix, Prime or YouTube.

PHASE 4: WAIT ON WHAT THE TV CAN TELL US, MEASURE WHAT IT CANNOT
    The TV reports some things: is it on the network, its power state, which app is in front.
    Those waits are now CHECKS that end as soon as the answer is known:
      * a fully OFF TV never answers on the network (Wake-on-LAN cannot wake it): we give up after
        TV_WAKE_WINDOW seconds instead of the old ~40-90 s connect loop
      * a TV that is already ON skips the wake pauses and the post-wake settle (~8 s saved)
    It does NOT report "the profile picker is drawn" or "the title page is open". Those waits stay
    FIXED (pause()), but each is a span "tv wait <name>" that also records WHEN the app came to the
    front: real numbers for tuning the fixed waits, with no change in behaviour.
"""
import asyncio
import time
import warnings

from aiowebostv import WebOsClient
from wakeonlan import send_magic_packet

import config
import tracing

TV_PORTS = (3001, 3000)       # webOS remote-control API: secure websocket, plain websocket


class TVUnreachable(RuntimeError):
    """The TV did not accept a connection (off, asleep, or not on the network)."""


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


async def answers(ip: str, ports=None, timeout: float = 1.0) -> bool:
    """Does the TV accept a TCP connection at all? (Quick: no handshake, no pairing.)"""
    for port in ports or TV_PORTS:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        except (OSError, asyncio.TimeoutError):
            continue
        writer.close()
        return True
    return False


async def wait_for_network(window: float) -> float | None:
    """After Wake-on-LAN: seconds until the TV answered on the network, or None within `window`."""
    ip = config.require("TV_IP")
    start = time.monotonic()
    with tracing.span("tv network check", {"tv.wait.kind": "check", "tv.wake_window_s": window}) as s:
        while (elapsed := time.monotonic() - start) < window:
            if await answers(ip):
                tracing.set_attrs(s, {"tv.answered_after_s": round(elapsed, 1)})
                return elapsed
            send_wake()
            await asyncio.sleep(1)
        tracing.set_attrs(s, {"tv.answered_after_s": None, "tv.outcome": "no answer"})
    return None


def power_state(power) -> str:
    return str(power.get("state") if isinstance(power, dict) else power)


async def wait_until(name: str, check, timeout: float, interval: float = 0.5) -> float | None:
    """A CHECKED wait: poll `await check()` until True. Seconds it took, or None at the timeout."""
    start = time.monotonic()
    with tracing.span(f"tv wait {name}", {"tv.wait.kind": "check", "tv.wait.timeout_s": timeout}) as s:
        while True:
            elapsed = time.monotonic() - start
            if await check():
                tracing.set_attrs(s, {"tv.wait.took_s": round(elapsed, 1)})
                return elapsed
            if elapsed >= timeout:
                tracing.set_attrs(s, {"tv.wait.took_s": None, "tv.outcome": "timed out"})
                return None
            await asyncio.sleep(interval)


async def pause(name: str, seconds: float, ready=None):
    """A FIXED wait, for what the TV cannot report. With `ready` (a plain function), also record WHEN it
    first became true, e.g. 'Netflix is the foreground app': data for tuning `seconds`, no change in
    behaviour (the full wait still happens)."""
    start, seen = time.monotonic(), None
    with tracing.span(f"tv wait {name}", {"tv.wait.kind": "fixed", "tv.wait.seconds": seconds}) as s:
        while (elapsed := time.monotonic() - start) < seconds:
            if ready is not None and seen is None and ready():
                seen = elapsed
            await asyncio.sleep(min(0.5, seconds - elapsed))
        if ready is not None:
            tracing.set_attrs(s, {"tv.ready_after_s": None if seen is None else round(seen, 1)})


async def connect_with_wake(max_wait: int = 90) -> WebOsClient:
    """Always wake first, then retry connecting until the TV accepts.

    Waking first matters: in standby some TVs still accept the network
    connection while the screen stays off, so waiting for a failed connect
    before sending the packet would never wake them.
    Phase 4: a quick network check first (fail fast when the TV is fully off), and the
    post-wake settle only when the TV was actually waking up.
    """
    for _ in range(3):
        send_wake()
        await asyncio.sleep(0.2)
    if await wait_for_network(config.TV_WAKE_WINDOW) is None:
        raise TVUnreachable(
            f"no answer on the network within {config.TV_WAKE_WINDOW}s after Wake-on-LAN: the TV is "
            "probably fully off (Wake-on-LAN only wakes it from standby) or not on this network")

    waited = 0
    last_error = "none"
    while waited < max_wait:
        try:
            c = await asyncio.wait_for(connect(), 8)
            state = power_state(await c.get_power_state())
            print("Connected. Power state:", state)
            if state != "Active":                       # it was waking: wait for the screen, then settle
                async def screen_on():
                    return power_state(await c.get_power_state()) == "Active"
                await wait_until("screen on", screen_on, timeout=20)
                await pause("after wake", config.POST_WAKE_WAIT)
            return c
        except Exception as e:  # noqa: BLE001
            waited += 10
            last_error = f"{type(e).__name__}: {e}".rstrip(": ")
            print(f"  waiting for TV... ({waited}s) {last_error}")
            send_wake()
            await asyncio.sleep(2)
    raise TVUnreachable(f"TV did not come up after wake (last error: {last_error})")


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


def profile_keys(position: int, layout: str) -> list[str]:
    """Keys that pick the profile at `position` (1 = first), whatever is highlighted now.

    layout "column": profiles stacked, first at the top (UP/DOWN).
    layout "row"   : profiles side by side, first on the left (LEFT/RIGHT).
    """
    back, forward = {"column": ("UP", "DOWN"), "row": ("LEFT", "RIGHT")}[layout]
    return [back] * config.PROFILE_RESET_PRESSES + [forward] * (position - 1) + ["ENTER"]


async def close_app(c: WebOsClient, app: str):
    """Close an app whether or not it is in front (it may be running in the background).

    UNVERIFIED on this TV: if close_app fails, we print a note and carry on.
    """
    try:
        await c.close_app(app)
    except Exception as e:  # noqa: BLE001
        print("  close_app failed or app not running, continuing:", type(e).__name__, e)
    await pause(f"{app} closing", 3, ready=lambda: c.tv_state.current_app_id != app)


async def close_app_if_open(c: WebOsClient, app: str):
    """Close an app if it is the foreground app, so the next launch starts fresh.

    UNVERIFIED on this TV: if close_app fails, we print a note and carry on.
    """
    if c.tv_state.current_app_id == app:
        try:
            await c.close_app(app)
        except Exception as e:  # noqa: BLE001
            print("  close_app failed, continuing:", type(e).__name__, e)
        await pause(f"{app} closing", 3, ready=lambda: c.tv_state.current_app_id != app)