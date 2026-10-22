"""
PKCE login and per-server tokens (Phase 2, Step 6).

Uses a scripted browser (kc_browser.py) on the REAL Keycloak login page, and a
temporary .login.json, so your own login is never touched.
Needs Keycloak running (skipped otherwise).

Run:  python tests/test_login.py        (or: pytest tests)
"""
import asyncio
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kc_browser
import login
from auth import AuthError, TokenVerifier
from test_auth import GAURAV, MISHIKA, Skip, keycloak_token
from test_catalog_http import URL, catalog_server, last_auth_log_line

login.STATE_FILE = Path(tempfile.mkdtemp()) / ".login.json"   # never your real login


def need_keycloak():
    keycloak_token("gaurav", "openid")   # raises Skip if Keycloak is down


def fresh_login(username, password=None, open_browser=None):
    if login.STATE_FILE.exists():
        login.STATE_FILE.unlink()
    return login.login(open_browser=open_browser or kc_browser.browser_for(username, password))


def accepted_at(token):
    ok = []
    for aud in ("catalog-mcp", "device-mcp", "taste-mcp"):
        try:
            TokenVerifier(aud).verify(token)
            ok.append(aud)
        except AuthError:
            pass
    return ok


# ----------------------------------------------------------------- tests
def test_password_grant_is_turned_off():
    # OAuth 2.1 removes it: the app would see the password. Only the PKCE login remains.
    need_keycloak()
    try:
        login._post_token({"grant_type": "password", "client_id": login.CLIENT_ID,
                           "username": "gaurav", "password": "gaurav-dev-pass", "scope": "openid"})
    except login.LoginError as e:
        assert "unauthorized_client" in str(e), e
        return
    raise AssertionError("password grant still works; set directAccessGrantsEnabled false")


def test_login_saves_who_logged_in():
    need_keycloak()
    state = fresh_login("mishika")
    assert state["username"] == "mishika" and state["sub"] == MISHIKA
    assert "access_token" not in login.STATE_FILE.read_text()   # only the refresh token is kept


def test_each_access_token_opens_exactly_one_server():
    need_keycloak()
    fresh_login("gaurav")
    for aud in ("catalog-mcp", "device-mcp", "taste-mcp"):
        token = login.access_token_for(aud)
        assert accepted_at(token) == [aud], (aud, accepted_at(token))
        assert TokenVerifier(aud).verify(token).user_id == GAURAV


def test_wrong_password_logs_nobody_in():
    need_keycloak()
    try:
        fresh_login("gaurav", password="wrong")
    except login.LoginError as e:
        assert "Invalid username or password" in str(e), e
        assert not login.STATE_FILE.exists()
        return
    raise AssertionError("login succeeded with a wrong password")


def test_answer_for_another_login_attempt_is_ignored():
    # The callback carries `state`. A code delivered with a different state (e.g. an
    # attacker's link) must not be accepted, even though the code itself is real.
    need_keycloak()

    def tampering_browser(url):
        callback = kc_browser.sign_in(url, "mishika", "mishika-dev-pass")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(callback).query))
        q["state"] = "someone-elses-state"
        urllib.request.urlopen(login.REDIRECT_URI + "?" + urllib.parse.urlencode(q), timeout=10).read()

    import urllib.error, urllib.request
    try:
        fresh_login("mishika", open_browser=lambda url: _ignore_400(tampering_browser, url))
    except login.LoginError as e:
        assert "State mismatch" in str(e), e
        assert not login.STATE_FILE.exists()
        return
    raise AssertionError("a callback with the wrong state was accepted")


def _ignore_400(fn, url):
    import urllib.error
    try:
        fn(url)
    except urllib.error.HTTPError as e:   # our callback page answers 400 for a bad state
        assert e.code == 400


def test_stolen_code_is_useless_without_the_verifier():
    # PKCE's whole point: the code travels through the browser and could leak,
    # but only the program holding the code_verifier can exchange it.
    need_keycloak()
    verifier, challenge = login.pkce_pair()
    callback = kc_browser.sign_in(login.authorize_url(challenge, "x"), "gaurav", "gaurav-dev-pass")
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(callback).query))["code"]
    attacker_verifier, _ = login.pkce_pair()
    try:
        login.exchange_code(code, attacker_verifier)
    except login.LoginError as e:
        assert "invalid_grant" in str(e), e
    else:
        raise AssertionError("code was exchanged with the wrong verifier")
    # Keycloak also burns a code after ONE attempt, so a guesser gets a single try,
    # and after a stolen code is tried even the real holder must log in again.
    try:
        login.exchange_code(code, verifier)
    except login.LoginError as e:
        assert "Code not valid" in str(e), e
    else:
        raise AssertionError("code was usable a second time")


def test_logout_ends_the_session_at_keycloak():
    need_keycloak()
    fresh_login("gaurav")
    old_refresh = login.load_login()["refresh_token"]
    login.logout()
    assert not login.STATE_FILE.exists()
    try:
        login._post_token({"grant_type": "refresh_token", "client_id": login.CLIENT_ID,
                           "refresh_token": old_refresh, "scope": "openid catalog-mcp"})
    except login.LoginError as e:
        assert "invalid_grant" in str(e), e
        return
    raise AssertionError("refresh token still works after logout")


def test_no_login_gives_a_clear_message():
    if login.STATE_FILE.exists():
        login.STATE_FILE.unlink()
    try:
        login.access_token_for("catalog-mcp")
    except login.LoginError as e:
        assert "python login.py" in str(e)
        return
    raise AssertionError("got a token with nobody logged in")


def test_agent_reaches_catalog_over_http_as_the_logged_in_person():
    need_keycloak()
    import agent
    fresh_login("mishika")

    async def discover():
        async with agent.McpToolbox({"catalog": URL}) as toolbox:
            return [t["function"]["name"] for t in toolbox.tools], toolbox.transports["catalog"]

    with catalog_server():
        names, transport = asyncio.run(discover())
        assert names == ["search_title", "search_youtube"], names
        assert transport == "http, as mishika", transport
        assert "accepted for mishika" in last_auth_log_line()


# ----------------------------------------------------------------- runner
if __name__ == "__main__":
    passed = failed = skipped = 0
    for name, fn in list(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Skip as e:
            print(f"SKIP  {name}  ({e})")
            skipped += 1
        except Exception as e:
            print(f"FAIL  {name}  {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)