"""
A scripted stand-in for a person at a browser, for tests only.

It follows the real PKCE login: opens login.py's authorize URL, fills in
Keycloak's login form, and stops at the redirect to 127.0.0.1:8765/callback.
So tests use the SAME flow as a real login, without a real browser.
"""
import html
import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import login


class _StopAtCallback(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith(login.REDIRECT_URI):
            raise _Arrived(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Arrived(Exception):
    def __init__(self, url):
        self.url = url


def sign_in(url: str, username: str, password: str) -> str:
    """Do what a person does on Keycloak's page. Returns the callback URL Keycloak sends the browser to."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _StopAtCallback)
    try:
        page = opener.open(url, timeout=10).read().decode()
        # Keycloak marks its cookies Secure. Browsers still send those to http://localhost;
        # Python's cookie jar does not, so we clear the flag (dev, localhost only).
        for cookie in jar:
            cookie.secure = False
        action = html.unescape(re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page).group(1))
        form = urllib.parse.urlencode({"username": username, "password": password, "credentialId": ""}).encode()
        page = opener.open(action, form, timeout=10).read().decode()
    except _Arrived as a:
        return a.url
    error = re.search(r"kc-feedback-text\">\s*([^<]+)", page)
    raise login.LoginError(f"login page did not redirect ({error.group(1).strip() if error else 'unknown reason'})")


def browser_for(username: str, password: str | None = None):
    """An open_browser replacement for login.login(): signs in and follows the redirect."""
    def open_browser(url):
        callback = sign_in(url, username, password or f"{username}-dev-pass")
        urllib.request.urlopen(callback, timeout=10).read()   # reaches login.py's callback server
    return open_browser


_refresh_tokens: dict[str, str] = {}   # username -> latest refresh token (they rotate)


def token(username: str, scope: str) -> str:
    """An access token for `username` narrowed to `scope`, via the real PKCE flow.

    Logs in once per user (scripted browser + code exchange, no callback server
    needed), then narrows with the refresh token, exactly as login.access_token_for does.
    """
    if username not in _refresh_tokens:
        verifier, challenge = login.pkce_pair()
        callback = sign_in(login.authorize_url(challenge, "test"), username, f"{username}-dev-pass")
        code = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(callback).query))["code"]
        _refresh_tokens[username] = login.exchange_code(code, verifier)["refresh_token"]
    tokens = login._post_token({"grant_type": "refresh_token", "client_id": login.CLIENT_ID,
                                "refresh_token": _refresh_tokens[username], "scope": scope})
    _refresh_tokens[username] = tokens.get("refresh_token", _refresh_tokens[username])
    return tokens["access_token"]