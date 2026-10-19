"""
device-mcp over HTTP: only a device-mcp token may reach the TV (Phase 2, Step 7).

This is the plan's rule "the Catalog agent must not be able to control the TV",
enforced by the network, not by the agent behaving well: a catalog-mcp token is
refused with 401 before any TV code runs.

The server is started with DEVICE_DRY_RUN=1, so the TV is never touched.
Needs Keycloak running (skipped otherwise).

Run:  python tests/test_device_http.py        (or: pytest tests)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

import config
from test_auth import Skip, keycloak_token
from test_catalog_http import http_server, last_auth_log_line, post_initialize

URL = f"http://127.0.0.1:{config.DEVICE_MCP_PORT}/mcp"
SERVER_LOG = config.BASE_DIR / "logs" / "test_device_http.log"


def device_server():
    # DEVICE_DRY_RUN=1 here wins over your .env: these tests must never drive the real TV.
    return http_server("device_mcp.py", config.DEVICE_MCP_PORT, SERVER_LOG,
                       {"DEVICE_DRY_RUN": "1", "TV_IP": os.environ.get("TV_IP") or "192.0.2.1",
                        "TV_MAC": os.environ.get("TV_MAC") or "AA:BB:CC:DD:EE:FF"})


def refused_because(token) -> str:
    assert post_initialize(token, URL)[0] == 401
    return last_auth_log_line(SERVER_LOG)


async def call_with(token: str, tool: str, args: dict):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(URL, http_client=http)) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        result = await client.call_tool(tool, args)
        text = "\n".join(getattr(b, "text", "") or "" for b in result.content)
        return names, json.loads(text)


# ----------------------------------------------------------------- tests
def test_no_token_is_refused():
    assert post_initialize(None, URL)[0] == 401


def test_catalog_token_cannot_control_the_tv():
    # THE Phase 2 rule. Same person, valid login, valid signature: still refused,
    # because this token was issued for the catalog, not for the TV.
    assert "WRONG_AUDIENCE" in refused_because(keycloak_token("gaurav", "openid catalog-mcp"))


def test_taste_token_cannot_control_the_tv():
    assert "WRONG_AUDIENCE" in refused_because(keycloak_token("mishika", "openid taste-mcp"))


def test_token_for_all_other_servers_still_cannot():
    token = keycloak_token("gaurav", "openid catalog-mcp taste-mcp")
    assert "WRONG_AUDIENCE" in refused_because(token)


def test_device_token_can_play_and_the_server_knows_who_asked():
    token = keycloak_token("mishika", "openid device-mcp")
    names, result = asyncio.run(call_with(token, "play_title",
                                          {"service": "netflix", "content_id": "82182295", "title": "The Friend"}))
    assert sorted(names) == ["play_title", "tv_status"], names
    assert result["status"] == "dry_run", result          # the TV was not touched
    time.sleep(0.2)
    assert "play_title requested by mishika" in SERVER_LOG.read_text(encoding="utf-8")


# ----------------------------------------------------------------- runners
try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _server():
        with device_server():
            yield
except ImportError:
    pass

if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    try:
        with device_server():
            for name, fn in tests:
                try:
                    fn()
                    print(f"PASS  {name}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {name}  {type(e).__name__}: {e}")
                    failed += 1
    except Skip as e:
        print(f"SKIP  all {len(tests)} tests ({e})")
    print(f"\n{passed} passed, {failed} failed   (server log: {SERVER_LOG})")
    sys.exit(1 if failed else 0)