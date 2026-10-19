"""
catalog-mcp over HTTP refuses every request without a valid catalog-mcp token
(Phase 2, Step 5).

Starts `python catalog_mcp.py --http` itself, then knocks on the door with:
no token, garbage, a token for ANOTHER server (device-mcp), and a correct one.
Only the correct one may get in. The server's own log line says WHY each was refused.

Needs Keycloak running (skipped otherwise). Makes no catalog lookups, so it uses
no RapidAPI quota.

Run:  python tests/test_catalog_http.py        (or: pytest tests)
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

import config
from test_auth import Skip, keycloak_token

URL = f"http://127.0.0.1:{config.CATALOG_MCP_PORT}/mcp"
SERVER_LOG = ROOT / "logs" / "test_catalog_http.log"


@contextmanager
def http_server(script: str, port: int, log_path: Path, extra_env: dict | None = None):
    """Start `python <script> --http` on 127.0.0.1:<port>, wait until it listens, stop it after."""
    keycloak_token("gaurav", "openid")   # skips everything if Keycloak is down
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Port {port} is already in use. Stop the other {script} --http first.")
    log_path.parent.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.setdefault("RAPIDAPI_KEY", "not-used-by-these-tests")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / script), "--http"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):                      # wait up to ~10 s for the port to open
            if proc.poll() is not None:
                raise RuntimeError(f"{script} --http exited early; see {log_path}")
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"{script} --http did not start; see {log_path}")
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


def catalog_server():
    return http_server("catalog_mcp.py", config.CATALOG_MCP_PORT, SERVER_LOG)


def post_initialize(token: str | None, url: str = URL):
    """A raw MCP 'initialize' request. Returns (status, www-authenticate header)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "0"}}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=10) as r:
            return r.status, r.headers.get("WWW-Authenticate", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("WWW-Authenticate", "")


def last_auth_log_line(log_path: Path = SERVER_LOG) -> str:
    time.sleep(0.2)
    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.startswith("[auth]")]
    return lines[-1] if lines else ""


async def list_tools_with(token: str) -> list[str]:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(URL, http_client=http)) as client:
        return [t.name for t in (await client.list_tools()).tools]


# ----------------------------------------------------------------- tests
def test_no_token_is_refused_and_told_where_to_log_in():
    status, www = post_initialize(None)
    assert status == 401, status
    # The 401 points the client at the metadata, which names Keycloak (MCP auth discovery).
    assert "resource_metadata=" in www, www
    meta_url = www.split('resource_metadata="')[1].split('"')[0]
    with urllib.request.urlopen(meta_url, timeout=10) as r:
        meta = json.load(r)
    assert meta["authorization_servers"] == [config.OIDC_ISSUER], meta
    assert meta["scopes_supported"] == ["catalog-mcp"], meta


def test_garbage_token_is_refused():
    assert post_initialize("not-a-token")[0] == 401
    assert "MALFORMED" in last_auth_log_line()


def test_token_for_device_mcp_is_refused():
    # The core Phase 2 rule, at the network level: a token for the TV server
    # does not open the catalog server (and the reverse will hold for device-mcp).
    token = keycloak_token("gaurav", "openid device-mcp")
    assert post_initialize(token)[0] == 401
    assert "WRONG_AUDIENCE" in last_auth_log_line()


def test_token_without_any_server_scope_is_refused():
    assert post_initialize(keycloak_token("gaurav", "openid"))[0] == 401
    assert "WRONG_AUDIENCE" in last_auth_log_line()


def test_edited_token_is_refused():
    import base64
    header, payload, sig = keycloak_token("mishika", "openid catalog-mcp").split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "11111111-1111-4111-8111-111111111111"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    assert post_initialize(f"{header}.{forged}.{sig}")[0] == 401
    assert "BAD_SIGNATURE" in last_auth_log_line()


def test_catalog_token_gets_in():
    token = keycloak_token("gaurav", "openid catalog-mcp")
    assert post_initialize(token)[0] == 200
    assert asyncio.run(list_tools_with(token)) == ["search_title"]
    assert "accepted for gaurav" in last_auth_log_line()


# ----------------------------------------------------------------- runners
try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _server():
        with catalog_server():
            yield
except ImportError:
    pass

if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    try:
        with catalog_server():
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