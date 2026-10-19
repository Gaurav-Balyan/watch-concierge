"""
device-mcp: an MCP server that controls the living-room TV.

The catalog server only READS data. This one ACTS on the real world, so its
design is about safety and honesty:

  * DRY RUN by default. While config.DEVICE_DRY_RUN is True (the default), the
    server never touches the TV: play_title only reports what it would do.
  * VALIDATION. The model supplies the arguments, so we check every one before
    it gets anywhere near the TV code. A model can be wrong, or be fed hostile
    text by a tool result (prompt injection, Phase 5). Never trust arguments.
  * FEWER KNOBS. The model chooses WHAT to play, never HOW. Things like "press
    Play on Prime" stay in code (services/*.DEFAULT_PLAY), not in the tool schema.
  * HONEST RESULTS. We can send a link and press keys, but the TV cannot tell us
    the title is playing. Results say "requested" and verified=False, never
    "playing".
  * CLASSIFIED FAILURES. Each failure says whether retrying is sensible. This
    matters because a blind retry could launch a title twice.
  * ONE ACTION AT A TIME. A second call while one is running is rejected.

stdout WARNING: this is a stdio server, and tv.py prints progress. The SDK
diverts stdout while serving, but that is best-effort, so every TV action also
runs with stdout redirected into the log file (logs/device_mcp.log), which is
also where you look when something fails.

HTTP (Phase 2, Step 7): `python device_mcp.py --http` listens on 127.0.0.1:DEVICE_MCP_PORT
and accepts only Keycloak tokens issued FOR device-mcp. A catalog-mcp token is refused
with 401 before any TV code runs: the catalog side can never control the TV.
Every play_title logs WHO asked (the token's user), the start of an audit trail.

Run it on its own (it just waits for a client), or test with the probe:
    python mcp_probe.py device
    python mcp_probe.py device tv_status
    python mcp_probe.py device play_title service=netflix content_id=82182295
"""
import asyncio
import contextlib
import re
import sys
from typing import Literal

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import config
import tv
from auth import McpTokenVerifier, caller_name
from logsetup import LogStream, get_logger
from services import SERVICES

AUDIENCE = "device-mcp"   # this server's name in Keycloak (a client scope in household-realm.json)

# The auth settings only take effect over HTTP; stdio ignores them.
mcp = MCPServer(
    "device",
    token_verifier=McpTokenVerifier(AUDIENCE),
    auth=AuthSettings(
        issuer_url=config.OIDC_ISSUER,
        resource_server_url=f"http://127.0.0.1:{config.DEVICE_MCP_PORT}/mcp",
        required_scopes=[AUDIENCE],                  # the token's scope must include device-mcp
        validate_token_resource=False,               # our verifier already checks the audience itself
    ),
)
log = get_logger("device_mcp")   # see logs/device_mcp.log

ACTION_TIMEOUT = 240          # seconds: wake + app launch + profile + link can take a while
_action_lock = asyncio.Lock()  # one TV action at a time

# Exactly what search_title can return. Anything else is rejected.
ID_PATTERNS = {
    "netflix": re.compile(r"^\d{1,12}$"),
    "prime": re.compile(r"^amzn1\.dv\.gti\.[0-9a-fA-F-]{8,64}$"),
}
ID_HINTS = {
    "netflix": "digits only, e.g. 82182295",
    "prime": "an id like amzn1.dv.gti.<uuid> (not a link)",
}


@contextlib.contextmanager
def stdout_to_log():
    """Send any print() made by TV code into the log, keeping stdout for the protocol."""
    stream = LogStream(log)
    try:
        with contextlib.redirect_stdout(stream):
            yield
    finally:
        stream.flush()


# ---------------------------------------------------------------- Read-only tool

@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
async def tv_status() -> dict:
    """Check whether the TV is reachable and what it is doing. Read-only: this
    does NOT wake the TV or change anything.

    Returns reachable (bool), power_state, foreground_app and dry_run_mode.
    reachable=False means the TV did not answer (it may be off or in deep standby);
    that is normal information, not an error.
    """
    with stdout_to_log():
        try:
            c = await asyncio.wait_for(tv.connect(), 8)
        except Exception:  # noqa: BLE001
            return {
                "reachable": False,
                "dry_run_mode": config.DEVICE_DRY_RUN,
                "note": "No answer from the TV. It may be off or in deep standby. "
                        "play_title wakes it automatically.",
            }
        try:
            power = await c.get_power_state()
            app = c.tv_state.current_app_id
        finally:
            with contextlib.suppress(Exception):
                await c.disconnect()
    state = power.get("state") if isinstance(power, dict) else str(power)
    return {"reachable": True, "power_state": state, "foreground_app": app,
            "dry_run_mode": config.DEVICE_DRY_RUN}


# ---------------------------------------------------------------- Action tool

@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True))
async def play_title(service: Literal["netflix", "prime"], content_id: str, title: str = "") -> dict:
    """Open a title on the living-room TV, waking the TV first if needed.

    Only call this with a service and content_id returned by search_title.
    This changes what is on the TV, and a repeated call can launch the title
    again, so do not call it twice for the same request.

    The result never claims the title is playing, because the TV cannot confirm
    that. status "requested" means the commands were sent.

    Args:
        service: Which service to open the title on.
        content_id: The id for that service, exactly as returned by search_title.
        title: The title name, for logging only.
    """
    # 1. VALIDATE before anything else. Pydantic already enforced the service enum.
    content_id = content_id.strip()
    if not ID_PATTERNS[service].match(content_id):
        raise ToolError(
            f"INVALID_INPUT: content_id {content_id!r} is not a valid {service} id "
            f"({ID_HINTS[service]}). Use an id returned by search_title. "
            "Retrying with the same input will fail again."
        )
    svc = SERVICES[service]
    log.info("play_title requested by %s: service=%s content_id=%s title=%r dry_run=%s",
             caller_name(), service, content_id, title, config.DEVICE_DRY_RUN)

    # 2. DRY RUN: touch nothing.
    if config.DEVICE_DRY_RUN:
        return {
            "status": "dry_run",
            "dry_run": True,
            "command_sent": False,
            "service": service,
            "content_id": content_id,
            "title": title,
            "message": "Dry-run mode: no command was sent to the TV.",
        }

    # 3. ONE ACTION AT A TIME.
    if _action_lock.locked():
        raise ToolError(
            "BUSY: another TV action is still running. Do not start a second one; "
            "wait for the first to finish."
        )

    async with _action_lock:
        try:
            await asyncio.wait_for(_open(svc, content_id), ACTION_TIMEOUT)
        except tv.TVUnreachable as e:
            log.error("play_title TV_UNREACHABLE: %s", e)
            raise ToolError(
                f"TV_UNREACHABLE: {e}. Nothing was launched. "
                "Retrying is reasonable once the TV is on and on the network."
            ) from e
        except (asyncio.TimeoutError, TimeoutError) as e:
            log.error("play_title TIMEOUT after %ss", ACTION_TIMEOUT)
            raise ToolError(
                f"TIMEOUT: the action did not finish within {ACTION_TIMEOUT}s. "
                "The TV state is UNKNOWN. Do not retry automatically; ask the user to check the TV."
            ) from e
        except Exception as e:  # noqa: BLE001
            log.exception("play_title crashed")
            raise ToolError(
                f"UNKNOWN_STATE: the action failed part-way ({type(e).__name__}). "
                "The TV state is UNKNOWN. Do not retry automatically; ask the user to check the TV."
            ) from e

    pressed_play = bool(svc.DEFAULT_PLAY)
    log.info("play_title requested OK: service=%s pressed_play=%s", service, pressed_play)
    result = {
        "status": "requested",
        "verified": False,
        "service": service,
        "content_id": content_id,
        "title": title,
        "pressed_play": pressed_play,
        "message": (
            f"Commands were sent to open the title on {service}"
            + (" and press Play" if pressed_play
               else ". Play was NOT pressed, so nothing is playing yet: the title page should be "
                    "open and someone must press Play")
            + ". Playback was NOT verified: the TV does not report it."
        ),
    }
    if not pressed_play:
        # A model tends to compress a long message, so the thing the user must do
        # gets its own field instead of living in the middle of a sentence.
        result["next_step_for_user"] = (
            "The title page should be open on the TV, but Play was not pressed. "
            "The user needs to press Play with the remote."
        )
    return result


async def _open(svc, content_id: str):
    """Wake + connect + open the title, always disconnecting. All prints go to the log."""
    # The redirect must cover the WHOLE function, including the disconnect in
    # `finally`: a stray print there would also land on the protocol channel.
    with stdout_to_log():
        c = None
        try:
            c = await tv.connect_with_wake()
            await svc.open_title(c, content_id, press_play=svc.DEFAULT_PLAY)
        finally:
            if c is not None:
                with contextlib.suppress(Exception):
                    await c.disconnect()


if __name__ == "__main__":
    config.require("TV_IP")
    config.require("TV_MAC")
    mode = "DRY RUN (TV is never touched)" if config.DEVICE_DRY_RUN else "LIVE (will control the TV)"
    if "--http" in sys.argv:
        log.info("device-mcp starting on http://127.0.0.1:%s/mcp: %s (tokens from %s)",
                 config.DEVICE_MCP_PORT, mode, config.OIDC_ISSUER)
        mcp.run(transport="streamable-http", host="127.0.0.1", port=config.DEVICE_MCP_PORT)
    else:
        log.info("device-mcp starting on stdio: %s", mode)
        mcp.run()   # default transport: stdio