"""
Tests that tokens are rejected for the RIGHT reason (Phase 2, Step 4).

Part 1 runs offline: it makes its own signing key, so no Keycloak is needed.
Part 2 uses the real Keycloak (skipped if it is not running).

Run:  python tests/test_auth.py        (or: pytest tests)
"""
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth import AuthError, TokenVerifier

try:
    import pytest
    Skip = pytest.skip.Exception
except ImportError:
    class Skip(Exception):
        pass

ISSUER = "http://localhost:8080/realms/household"
GAURAV = "11111111-1111-4111-8111-111111111111"
MISHIKA = "22222222-2222-4222-8222-222222222222"

# ----------------------------------------------------------------- Part 1: offline
# Our stand-in for Keycloak: one key pair it "publishes" under kid "test-key".
ISSUER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLISHED = {"test-key": ISSUER_KEY.public_key()}


def published_key(token):
    """Behaves like the real JWKS lookup: find the key by the token's kid."""
    kid = jwt.get_unverified_header(token).get("kid")
    if kid not in PUBLISHED:
        raise jwt.PyJWKClientError(f'Unable to find a signing key that matches: "{kid}"')
    return PUBLISHED[kid]


def make_token(key=ISSUER_KEY, kid="test-key", alg="RS256", **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "device-mcp", "sub": GAURAV, "azp": "concierge-cli",
              "preferred_username": "gaurav", "scope": "openid profile device-mcp",
              "iat": now, "exp": now + 300}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}  # None = leave the claim out
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def verifier(audience="device-mcp"):
    return TokenVerifier(audience, issuer=ISSUER, key_for_token=published_key)


def rejected(token, audience="device-mcp"):
    """Returns the AuthError code. Fails the test if the token was ACCEPTED."""
    try:
        caller = verifier(audience).verify(token)
    except AuthError as e:
        return e.code
    raise AssertionError(f"token was accepted but should not be: {caller}")


def b64(part: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()


def test_good_token_is_accepted():
    caller = verifier().verify(make_token())
    assert caller.user_id == GAURAV and caller.username == "gaurav"
    assert "device-mcp" in caller.scopes


def test_token_for_another_server_is_rejected():
    # The core rule: a token meant for catalog-mcp cannot be used at device-mcp.
    assert rejected(make_token(aud="catalog-mcp")) == AuthError.WRONG_AUDIENCE


def test_token_with_no_audience_is_rejected():
    # What you got from Keycloak with scope=openid only.
    assert rejected(make_token(aud=None)) == AuthError.WRONG_AUDIENCE


def test_token_for_several_servers_is_accepted_by_each():
    token = make_token(aud=["taste-mcp", "catalog-mcp"])
    assert verifier("taste-mcp").verify(token).user_id == GAURAV
    assert verifier("catalog-mcp").verify(token).user_id == GAURAV
    assert rejected(token, "device-mcp") == AuthError.WRONG_AUDIENCE


def test_expired_token_is_rejected():
    long_ago = int(time.time()) - 3600
    assert rejected(make_token(iat=long_ago, exp=long_ago + 300)) == AuthError.EXPIRED


def test_token_from_another_issuer_is_rejected():
    assert rejected(make_token(iss="http://localhost:8080/realms/master")) == AuthError.WRONG_ISSUER


def test_token_without_subject_is_rejected():
    assert rejected(make_token(sub=None)) == AuthError.MISSING_CLAIM


def test_changing_the_user_breaks_the_signature():
    # Mishika takes Gaurav's token and edits sub to... anyone. The signature no longer matches.
    header, payload, signature = make_token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = MISHIKA
    assert rejected(f"{header}.{b64(claims)}.{signature}") == AuthError.BAD_SIGNATURE


def test_token_signed_by_an_attacker_key_is_rejected():
    # Attacker signs with their own key but claims to be the issuer's key id.
    assert rejected(make_token(key=ATTACKER_KEY)) == AuthError.BAD_SIGNATURE


def test_token_with_unknown_key_id_is_rejected():
    assert rejected(make_token(key=ATTACKER_KEY, kid="attacker-key")) == AuthError.UNKNOWN_KEY


def test_unsigned_token_is_rejected():
    # alg "none": a JWT with no signature at all. Must never be accepted.
    header = b64({"alg": "none", "typ": "JWT", "kid": "test-key"})
    now = int(time.time())
    payload = b64({"iss": ISSUER, "aud": "device-mcp", "sub": GAURAV, "iat": now, "exp": now + 300})
    assert rejected(f"{header}.{payload}.") == AuthError.BAD_SIGNATURE


def test_hs256_with_public_key_as_secret_is_rejected():
    # Classic attack: the public key is public, so sign with HS256 using it as the
    # shared secret. A verifier that trusts the token's own "alg" would accept this.
    public_pem = ISSUER_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())
    header = b64({"alg": "HS256", "typ": "JWT", "kid": "test-key"})
    payload = b64({"iss": ISSUER, "aud": "device-mcp", "sub": GAURAV, "iat": now, "exp": now + 300})
    import hmac, hashlib
    sig = base64.urlsafe_b64encode(
        hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert rejected(f"{header}.{payload}.{sig}") == AuthError.BAD_SIGNATURE


def test_garbage_is_rejected():
    assert rejected("not-a-token") == AuthError.MALFORMED


# ----------------------------------------------------------------- Part 2: real Keycloak
def keycloak_token(username: str, scope: str) -> str:
    """A real token from Keycloak, through the PKCE login (Step 6 turned the password grant off)."""
    import urllib.error
    import kc_browser
    import login
    try:
        return kc_browser.token(username, scope)
    except (urllib.error.URLError, ConnectionError) as e:
        raise Skip(f"Keycloak not reachable at {login.config.OIDC_ISSUER} ({e})")
    except login.LoginError as e:
        if "not reachable" in str(e):
            raise Skip(str(e))
        raise


def real_rejected(token, audience):
    try:
        caller = TokenVerifier(audience).verify(token)
    except AuthError as e:
        return e.code
    raise AssertionError(f"token was accepted but should not be: {caller}")


def test_live_device_token_works_only_at_device_mcp():
    token = keycloak_token("gaurav", "openid device-mcp")
    assert TokenVerifier("device-mcp").verify(token).user_id == GAURAV
    assert real_rejected(token, "catalog-mcp") == AuthError.WRONG_AUDIENCE
    assert real_rejected(token, "taste-mcp") == AuthError.WRONG_AUDIENCE


def test_live_token_without_scope_works_nowhere():
    token = keycloak_token("gaurav", "openid")
    for audience in ("catalog-mcp", "device-mcp", "taste-mcp"):
        assert real_rejected(token, audience) == AuthError.WRONG_AUDIENCE


def test_live_each_person_gets_their_own_id():
    assert TokenVerifier("taste-mcp").verify(keycloak_token("gaurav", "openid taste-mcp")).user_id == GAURAV
    assert TokenVerifier("taste-mcp").verify(keycloak_token("mishika", "openid taste-mcp")).user_id == MISHIKA


def test_live_edited_token_is_rejected():
    header, payload, signature = keycloak_token("mishika", "openid taste-mcp").split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = GAURAV   # Mishika tries to become Gaurav
    assert real_rejected(f"{header}.{b64(claims)}.{signature}", "taste-mcp") == AuthError.BAD_SIGNATURE


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