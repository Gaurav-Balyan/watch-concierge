"""
Log in as a household member (Phase 2, Step 6): OAuth 2.1 authorization code + PKCE.

    python -m common.login            # opens the browser; sign in as gaurav or mishika
    python -m common.login --status   # who is logged in, and until when
    python -m common.login --logout   # forget the login (and end the Keycloak session)

HOW IT WORKS (the concept)
    1. We make a random secret, the code_verifier, and send Keycloak only its
       SHA-256 hash (the code_challenge) in the browser login link.
    2. You type your password into KEYCLOAK's page, never into this program.
    3. Keycloak redirects the browser to http://127.0.0.1:8765/callback?code=...
       where a tiny one-shot web server in this script catches the code.
    4. We swap code + code_verifier for tokens. Someone who stole the code from
       the redirect cannot use it: they do not have the verifier (that is PKCE).
    5. We keep only the REFRESH token, in .login.json (git-ignored). Before each
       call to an MCP server, the agent trades it for a short-lived ACCESS token
       narrowed to that one server, e.g. scope "openid catalog-mcp" -> aud catalog-mcp.

The agent uses access_token_for(audience). If nobody is logged in, it says so.
"""
import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import httpx2            # the HTTP library the mcp SDK uses (comes with mcp)

from common import config

CLIENT_ID = "concierge-cli"
REDIRECT_URI = "http://127.0.0.1:8765/callback"
# At login we ask for every server scope ONCE; later each access token is narrowed to one.
# Phase 3: taste-household-read is asked for at LOGIN so that a later refresh may include it
# (a refresh can only NARROW what the login granted, never widen it). Only the Consensus agent's
# token actually carries it: access_token_for(..., extra_scopes=[...]).
LOGIN_SCOPE = "openid catalog-mcp device-mcp taste-mcp taste-household-read"
STATE_FILE = config.BASE_DIR / ".login.json"
LOGIN_TIMEOUT = 180   # seconds to finish signing in in the browser

AUTHORIZE_URL = f"{config.OIDC_ISSUER}/protocol/openid-connect/auth"
TOKEN_URL = f"{config.OIDC_ISSUER}/protocol/openid-connect/token"
LOGOUT_URL = f"{config.OIDC_ISSUER}/protocol/openid-connect/logout"


class LoginError(Exception):
    pass


# ----------------------------------------------------------------- PKCE pieces
def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge). The challenge is the base64url SHA-256 of the verifier."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": LOGIN_SCOPE,
        "state": state,                      # ties the answer to THIS login attempt (CSRF protection)
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",                   # always show the login page, so you can pick who you are
    })


def _post_token(form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    try:
        with urllib.request.urlopen(TOKEN_URL, data, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)
            raise LoginError(f"{err.get('error')}: {err.get('error_description', '')}") from None
        except json.JSONDecodeError:
            raise LoginError(f"HTTP {e.code} from Keycloak: {body[:200]}") from None
    except urllib.error.URLError as e:
        raise LoginError(f"Keycloak not reachable at {config.OIDC_ISSUER} ({e.reason}). Is the container running?") from None


def exchange_code(code: str, verifier: str) -> dict:
    return _post_token({"grant_type": "authorization_code", "client_id": CLIENT_ID,
                        "code": code, "redirect_uri": REDIRECT_URI, "code_verifier": verifier})


def _claims(token: str) -> dict:
    """Read a token's claims WITHOUT checking it. Only for display; servers always verify."""
    p = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


# ----------------------------------------------------------------- saved login
def save_login(tokens: dict) -> dict:
    claims = _claims(tokens["access_token"])
    state = {"username": claims.get("preferred_username"), "sub": claims.get("sub"),
             "refresh_token": tokens["refresh_token"],
             "refresh_expires_at": int(time.time()) + int(tokens.get("refresh_expires_in", 0))}
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def load_login() -> dict | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def access_token_for(audience: str, extra_scopes: tuple[str, ...] = ()) -> str:
    """A fresh access token usable ONLY at `audience` (e.g. 'catalog-mcp').
    extra_scopes: permissions on top of the audience, e.g. ('taste-household-read',).

    Uses the saved refresh token and asks for a narrower scope than at login.
    Keycloak's refresh token rotates, so we save the new one each time.
    """
    state = load_login()
    if state is None:
        raise LoginError("Nobody is logged in. Run: python -m common.login")
    try:
        tokens = _post_token({"grant_type": "refresh_token", "client_id": CLIENT_ID,
                              "refresh_token": state["refresh_token"],
                              "scope": " ".join(("openid", audience, *extra_scopes))})
    except LoginError as e:
        if "invalid_grant" in str(e):
            raise LoginError(f"The saved login for {state['username']} has expired or was ended. "
                             "Run: python -m common.login") from None
        raise
    if "refresh_token" in tokens:
        save_login(tokens)
    return tokens["access_token"]


class RefreshingAuth(httpx2.Auth):
    """Phase 4: an access token that renews itself, for one MCP connection (one audience).

    Access tokens last 5 minutes; a long run (the team, a slow TV) used to keep the first one and get
    401s halfway. Now each HTTP request gets a token that is still valid:
      * PROACTIVE: shortly before `exp` (read from the token), fetch a new one (refresh grant)
      * REACTIVE:  if the server still answers 401 (clock skew, Keycloak restarted), fetch a new one
                   and send the request ONCE more. A second 401 is real: it goes back to the caller.
    `fetch` returns a token (login.access_token_for, narrowed to this server's audience)."""

    def __init__(self, fetch, margin: float = 30, clock=time.time):
        self.fetch, self.margin, self.clock = fetch, margin, clock
        self.token, self.expires_at = None, 0.0

    def current(self, force: bool = False) -> str:
        if force or self.token is None or self.clock() >= self.expires_at - self.margin:
            self.token = self.fetch()
            self.expires_at = float(_claims(self.token).get("exp", 0))
        return self.token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.current()}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self.current(force=True)}"
            yield request


# ----------------------------------------------------------------- browser login
def login(open_browser=webbrowser.open) -> dict:
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":
                self.send_error(404)
                return
            q = dict(urllib.parse.parse_qsl(url.query))
            result.update(q)
            ok = "code" in q and q.get("state") == state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Logged in. You can close this tab." if ok else "Login failed. See the terminal."
            self.wfile.write(f"<p style='font:16px sans-serif'>{msg}</p>".encode())

        def log_message(self, *args):   # keep the terminal quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8765), Callback)
    server.timeout = 1
    url = authorize_url(challenge, state)
    if open_browser is webbrowser.open:
        print("Opening the browser to sign in. If it does not open, paste this link:\n" + url, file=sys.stderr)
    def run_browser():
        try:
            open_browser(url)
        except Exception as e:  # noqa: BLE001  (tests pass a scripted browser that can fail)
            result.update(error="browser_failed", error_description=str(e))

    threading.Thread(target=run_browser, daemon=True).start()
    deadline = time.time() + LOGIN_TIMEOUT
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise LoginError(f"Keycloak refused the login: {result['error']} {result.get('error_description', '')}")
    if "code" not in result:
        raise LoginError(f"No login within {LOGIN_TIMEOUT}s.")
    if result.get("state") != state:
        raise LoginError("State mismatch: this answer was not for our login attempt. Ignored.")
    return save_login(exchange_code(result["code"], verifier))


def logout():
    state = load_login()
    if state is None:
        print("Nobody is logged in.")
        return
    try:   # end the session at Keycloak too, so the refresh token stops working everywhere
        urllib.request.urlopen(LOGOUT_URL, urllib.parse.urlencode(
            {"client_id": CLIENT_ID, "refresh_token": state["refresh_token"]}).encode(), timeout=10)
    except (urllib.error.URLError, OSError) as e:
        print(f"(Could not reach Keycloak to end the session: {e})")
    STATE_FILE.unlink()
    print(f"Logged out {state['username']}.")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if arg == "--status":
            s = load_login()
            if s is None:
                print("Nobody is logged in. Run: python -m common.login")
            else:
                left = s["refresh_expires_at"] - int(time.time())
                print(f"Logged in as {s['username']} ({s['sub']}); login valid for about {max(left, 0) // 60} more minutes "
                      "(each use extends it).")
        elif arg == "--logout":
            logout()
        elif arg == "":
            s = login()
            print(f"Logged in as {s['username']}.")
        else:
            sys.exit(__doc__)
    except LoginError as e:
        sys.exit(f"Login problem: {e}")


if __name__ == "__main__":
    main()