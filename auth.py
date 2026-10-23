"""
Token checking for MCP servers (Phase 2, Step 4).

An MCP server never asks Keycloak "is this token OK?". It downloads Keycloak's
public keys once (the JWKS) and checks every token itself:

  1. signature  - signed by the issuer's key, with RS256 only
  2. issuer     - `iss` is exactly our realm
  3. audience   - `aud` names THIS server (a token for catalog-mcp is useless at device-mcp)
  4. expiry     - `exp` is in the future
  5. subject    - `sub` is present (that is "who is asking")

Every failure raises AuthError with a fixed code, so tests can check that a token
was rejected for the RIGHT reason, not just rejected.

Try it:  python auth.py <audience> <token>
"""
import sys
from dataclasses import dataclass

import anyio
import jwt  # PyJWT
from mcp.server.auth.provider import AccessToken

import config

ALGORITHMS = ["RS256"]   # never accept "none" or HS256 (see test_auth.py for why)
LEEWAY_SECONDS = 30      # tolerated clock difference between Keycloak and this PC


class AuthError(Exception):
    """A rejected token. `code` is one of the constants below."""
    MALFORMED = "MALFORMED"            # not a JWT at all
    UNKNOWN_KEY = "UNKNOWN_KEY"        # signed with a key the issuer does not publish
    BAD_SIGNATURE = "BAD_SIGNATURE"    # content changed after signing, or wrong algorithm
    WRONG_ISSUER = "WRONG_ISSUER"
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    EXPIRED = "EXPIRED"
    MISSING_CLAIM = "MISSING_CLAIM"

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Caller:
    """Who is asking, taken from a verified token."""
    user_id: str      # `sub`: stable id, use this as the database key
    username: str     # `preferred_username`: for logs and messages only
    client: str       # `azp`: which app asked for the token
    scopes: frozenset
    expires_at: int = 0


class TokenVerifier:
    """Checks tokens for one MCP server (one audience).

    `key_for_token` is a function token -> public key. By default it fetches the
    issuer's JWKS (cached). Tests pass their own so they can run without Keycloak.
    """

    def __init__(self, audience: str, issuer: str = config.OIDC_ISSUER, key_for_token=None):
        self.audience = audience
        self.issuer = issuer
        if key_for_token is None:
            jwks = jwt.PyJWKClient(f"{issuer}/protocol/openid-connect/certs", cache_keys=True)
            key_for_token = lambda token: jwks.get_signing_key_from_jwt(token).key
        self._key_for_token = key_for_token

    def verify(self, token: str) -> Caller:
        try:
            key = self._key_for_token(token)
        except jwt.PyJWKClientConnectionError:
            raise  # Keycloak unreachable is an infrastructure problem, not a bad token
        except jwt.PyJWKClientError as e:
            raise AuthError(AuthError.UNKNOWN_KEY, str(e)) from None
        except jwt.DecodeError as e:
            raise AuthError(AuthError.MALFORMED, str(e)) from None

        try:
            claims = jwt.decode(
                token, key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError(AuthError.EXPIRED, str(e)) from None
        except jwt.InvalidAudienceError:
            raise AuthError(AuthError.WRONG_AUDIENCE, f"token is not for {self.audience}") from None
        except jwt.InvalidIssuerError as e:
            raise AuthError(AuthError.WRONG_ISSUER, str(e)) from None
        except jwt.MissingRequiredClaimError as e:
            # A token with no `aud` at all lands here; for us that is a wrong audience.
            code = AuthError.WRONG_AUDIENCE if e.claim == "aud" else AuthError.MISSING_CLAIM
            raise AuthError(code, str(e)) from None
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError) as e:
            raise AuthError(AuthError.BAD_SIGNATURE, str(e)) from None
        except jwt.DecodeError as e:
            raise AuthError(AuthError.MALFORMED, str(e)) from None

        return Caller(
            user_id=claims["sub"],
            username=claims.get("preferred_username", ""),
            client=claims.get("azp", ""),
            scopes=frozenset(claims.get("scope", "").split()),
            expires_at=claims["exp"],
        )


def caller_name() -> str:
    """Inside a tool: who sent this request. Over stdio there is no token: 'local (stdio)'."""
    from mcp.server.auth.middleware.auth_context import get_access_token
    token = get_access_token()
    if token is None:
        return "local (stdio)"
    return (token.claims or {}).get("preferred_username") or token.subject or "unknown"


class McpTokenVerifier:
    """Plugs TokenVerifier into an MCP server (Phase 2, Step 5).

    The MCP SDK calls verify_token() for every HTTP request. Returning None makes
    the SDK answer 401 Unauthorized before any tool code runs. We log WHY on stderr,
    because the 401 itself deliberately tells the caller nothing.
    """

    def __init__(self, audience: str):
        self.audience = audience
        self._verifier = TokenVerifier(audience)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # verify() may fetch Keycloak's keys over the network: run it off the event loop.
            caller = await anyio.to_thread.run_sync(self._verifier.verify, token)
        except AuthError as e:
            print(f"[auth] {self.audience}: token REJECTED ({e.code})", file=sys.stderr)
            return None
        except jwt.PyJWKClientConnectionError as e:
            print(f"[auth] {self.audience}: cannot reach Keycloak to get keys ({e})", file=sys.stderr)
            return None
        print(f"[auth] {self.audience}: token accepted for {caller.username}", file=sys.stderr)
        return AccessToken(
            token=token,
            client_id=caller.client,
            scopes=sorted(caller.scopes),
            expires_at=caller.expires_at,
            subject=caller.user_id,
            claims={"iss": self._verifier.issuer, "preferred_username": caller.username},
        )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python auth.py <audience> <token>")
    try:
        print("ACCEPTED", TokenVerifier(sys.argv[1]).verify(sys.argv[2]))
    except AuthError as e:
        print("REJECTED", e.code, "-", e)