"""
Fake versions of the two MCP servers, for evals.

They are the REAL servers (same tools, same validation, same error handling)
with the outside world swapped out:

  catalog modes:  ok            fixed catalog from fixtures.py
                  rate_limited  every search hits a simulated HTTP 429
                  down          the API does not answer; the REAL cache, retries and breaker run (Phase 4)
  device modes:   dryrun        the real dry-run behaviour
                  unreachable   the TV never answers (real TV_UNREACHABLE path)
                  live          a pretend TV that always succeeds, so play_title
                                returns its real "requested" result

Nothing here can reach your TV, RapidAPI or the network.
stdout belongs to the MCP protocol: never print() in this file.
"""
import _paths  # noqa: F401  (must come first: sets up sys.path)

import os
import sys
import tempfile
import types
from pathlib import Path

# Phase 4: the fakes keep their idempotency records, cache and breaker away from your real .state/.
# A FIXED folder (not one per process): a resumed drill run must see the records of the crashed one.
EVAL_STATE = Path(tempfile.gettempdir()) / "watch_concierge_eval_state"

import httpx


def _stderr_to_file():
    """Send this server's stderr (its own logging) to a file, not the terminal.

    The runner starts two servers per run, and their log lines would bury the
    progress output. They are not lost: see evals/results/eval_servers_stderr.log.
    Done at file-descriptor level so library loggers are silenced too.
    """
    results = _paths.HERE / "results"
    results.mkdir(exist_ok=True)
    fd = os.open(str(results / "eval_servers_stderr.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stderr = os.fdopen(os.dup(2), "w", buffering=1)


def serve_catalog(mode: str):
    _stderr_to_file()
    import catalog_mcp
    import lookup
    from fixtures import search

    if mode == "ok":
        lookup.find_title = search
        from fixtures import find_videos
        catalog_mcp.youtube.find_videos = find_videos     # no network for YouTube either
    elif mode == "down":
        # Phase 4: the OUTSIDE API is down, but the real cache + retries + circuit breaker run (lookup.py).
        import config
        config.STATE_DIR = EVAL_STATE

        async def outage(title):
            raise httpx.ConnectError("simulated outage: the catalog API does not answer")
        lookup._api_search = outage
    elif mode == "rate_limited":
        async def limited(title, year=None, only=None):
            request = httpx.Request("GET", "https://eval.invalid/search")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)
        lookup.find_title = limited
    else:
        raise ValueError(f"unknown catalog mode: {mode}")
    catalog_mcp.mcp.run()


def serve_device(mode: str):
    _stderr_to_file()
    import logsetup
    # Keep eval logging out of your real logs/device_mcp.log.
    logsetup.LOG_DIR = Path(tempfile.gettempdir()) / "watch_concierge_eval_logs"

    import config
    config.STATE_DIR = EVAL_STATE
    import device_mcp
    import tv
    from services import SERVICES

    class FakeTV:
        tv_state = types.SimpleNamespace(current_app_id="netflix")

        async def get_power_state(self):
            return {"returnValue": True, "state": "Active"}

        async def disconnect(self):
            pass

    async def fake_connect():
        return FakeTV()

    if mode == "dryrun":
        config.DEVICE_DRY_RUN = True
        tv.connect = fake_connect                    # used only by tv_status (read-only)
    elif mode == "unreachable":
        config.DEVICE_DRY_RUN = False

        async def down():
            raise OSError("no route to host")

        async def down_wake(**kwargs):
            raise tv.TVUnreachable("TV did not come up after wake")

        tv.connect = down
        tv.connect_with_wake = down_wake
    elif mode == "live":
        config.DEVICE_DRY_RUN = False
        tv.connect = fake_connect

        async def fake_wake(**kwargs):
            return FakeTV()

        async def noop_open(c, content_id, press_play):
            # Every pretend LAUNCH is written down: the crash drills count them (a duplicate = a bug).
            EVAL_STATE.mkdir(parents=True, exist_ok=True)
            with open(EVAL_STATE / "launches.log", "a", encoding="utf-8") as f:
                f.write(content_id + "\n")

        tv.connect_with_wake = fake_wake
        # Patch the dict device_mcp ACTUALLY uses (Phase 3: services.PLAYABLE, which adds YouTube).
        # Patching only services.SERVICES would leave device_mcp with the real service modules.
        for registry in (SERVICES, device_mcp.SERVICES):
            for name in list(registry):
                registry[name] = types.SimpleNamespace(NAME=name, DEFAULT_PLAY=True, open_title=noop_open)
    else:
        raise ValueError(f"unknown device mode: {mode}")
    device_mcp.mcp.run()