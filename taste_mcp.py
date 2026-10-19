"""
taste-mcp: each household member's likes, dislikes and watch history (Phase 2, Step 8).

SEPARATION OF PRIVILEGE, IN TWO LAYERS
    1. The tools have NO user argument. The user is ALWAYS the `sub` of the verified
       token (auth.McpTokenVerifier). So the model cannot ask for someone else's data,
       not by mistake and not because a malicious movie description told it to.
    2. Every query says WHERE user_id = <caller>. And the database enforces it AGAIN with
       row-level security (db/schema.psql): each transaction sets app.user_id to the
       caller, and Postgres only shows and accepts that person's rows. So a future query
       that forgets its WHERE still cannot leak. Each layer is tested on its own.
    And the server logs in as `taste_app`, a role that cannot bypass those rules,
    cannot delete history and cannot change the tables.

HTTP ONLY
    Over stdio there is no token, so there is no "who". Every tool then refuses.

Run:
    python taste_mcp.py --http      # 127.0.0.1:TASTE_MCP_PORT, needs Postgres (household-db) and Keycloak
"""
import sys
from typing import Literal

import anyio
import psycopg
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import config
from auth import McpTokenVerifier

AUDIENCE = "taste-mcp"
HISTORY_SHOWN = 10
MAX_ITEMS_PER_KIND = 50

mcp = MCPServer(
    "taste",
    token_verifier=McpTokenVerifier(AUDIENCE),
    auth=AuthSettings(
        issuer_url=config.OIDC_ISSUER,
        resource_server_url=f"http://127.0.0.1:{config.TASTE_MCP_PORT}/mcp",
        required_scopes=[AUDIENCE],
        validate_token_resource=False,
    ),
)


# ----------------------------------------------------------------- who is asking
def caller() -> tuple[str, str]:
    """(user_id, username) from the verified token. There is deliberately no other source."""
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolError("NOT_LOGGED_IN: taste tools need to know who is asking. "
                        "They only work over HTTP with a login (python login.py).")
    return token.subject, (token.claims or {}).get("preferred_username", "")


# ----------------------------------------------------------------- database
def _run(user_id: str, work):
    """Run work(cursor) in ONE transaction that the database scopes to user_id (row-level security)."""
    try:
        with psycopg.connect(config.TASTE_DB_URL, connect_timeout=5) as conn:
            with conn.transaction(), conn.cursor() as cur:
                # is_local=true: the setting ends with this transaction, so it can never leak
                # into a later request that reuses the connection.
                cur.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
                return work(cur)
    except psycopg.OperationalError as e:
        raise ToolError(f"Taste database unreachable ({type(e).__name__}). "
                        "This is a system problem; retrying immediately will not help.") from e


async def in_db(user_id: str, work):
    # psycopg's sync API in a worker thread: works on every OS and event loop.
    return await anyio.to_thread.run_sync(_run, user_id, work)


def _clean_item(item: str) -> str:
    item = " ".join(item.split())
    if not 1 <= len(item) <= 100:
        raise ToolError("INVALID_INPUT: item must be 1 to 100 characters, e.g. 'horror' or 'Christopher Nolan'.")
    return item


# ----------------------------------------------------------------- tools
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
async def get_my_taste() -> dict:
    """Get the likes, dislikes and recently watched titles of the person making this request.

    Always about the logged-in person; there is no way to ask about someone else.
    Use it before suggesting what to watch. Items are things the person said, e.g.
    genres, actors or titles.
    """
    user_id, username = caller()

    def work(cur):
        # Layer 1: every query names the caller explicitly. Layer 2 (row-level security)
        # would hide other people's rows even if this WHERE were forgotten.
        cur.execute("SELECT kind, item FROM taste_items WHERE user_id = %s ORDER BY created_at", (user_id,))
        items = cur.fetchall()
        cur.execute("SELECT title, service, watched_at FROM watch_history WHERE user_id = %s "
                    "ORDER BY watched_at DESC LIMIT %s", (user_id, HISTORY_SHOWN))
        history = cur.fetchall()
        return items, history

    items, history = await in_db(user_id, work)
    return {
        "person": username,
        "likes": [i for k, i in items if k == "like"],
        "dislikes": [i for k, i in items if k == "dislike"],
        "recently_watched": [{"title": t, "service": s, "watched_at": w.isoformat(timespec="minutes")}
                             for t, s, w in history],
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False))
async def remember_taste(kind: Literal["like", "dislike"], item: str) -> dict:
    """Remember something the person making this request likes or dislikes.

    Only call this when the person states a preference themselves, e.g.
    "I don't like horror" -> kind="dislike", item="horror".

    Args:
        kind: "like" or "dislike".
        item: A short thing: a genre, actor, director or title (1-100 characters).
    """
    user_id, username = caller()
    item = _clean_item(item)
    other = "dislike" if kind == "like" else "like"

    def work(cur):
        cur.execute("SELECT count(*) FROM taste_items WHERE user_id = %s AND kind = %s", (user_id, kind))
        if cur.fetchone()[0] >= MAX_ITEMS_PER_KIND:
            raise ToolError(f"LIMIT: {username} already has {MAX_ITEMS_PER_KIND} {kind}s saved.")
        # Liking something you said you disliked replaces the old opinion.
        cur.execute("DELETE FROM taste_items WHERE user_id = %s AND kind = %s AND lower(item) = lower(%s)",
                    (user_id, other, item))
        cur.execute("INSERT INTO taste_items (user_id, kind, item) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (user_id, kind, item))

    await in_db(user_id, work)
    return {"saved": True, "person": username, "kind": kind, "item": item}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False))
async def forget_taste(kind: Literal["like", "dislike"], item: str) -> dict:
    """Forget a like or dislike of the person making this request.

    Args:
        kind: "like" or "dislike".
        item: The item exactly as returned by get_my_taste.
    """
    user_id, username = caller()
    item = _clean_item(item)

    def work(cur):
        cur.execute("DELETE FROM taste_items WHERE user_id = %s AND kind = %s AND item = %s",
                    (user_id, kind, item))
        return cur.rowcount

    removed = await in_db(user_id, work)
    return {"removed": removed, "person": username, "kind": kind, "item": item}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False, open_world_hint=False))
async def record_watch(title: str, service: Literal["netflix", "prime"], content_id: str) -> dict:
    """Add a title to the watch history of the person making this request.

    The agent calls this itself after play_title succeeds; it is not for the model.

    Args:
        title: The title that was played.
        service: The service it was played on.
        content_id: The id it was played with.
    """
    user_id, username = caller()
    title = _clean_item(title)

    def work(cur):
        cur.execute("INSERT INTO watch_history (user_id, title, service, content_id) VALUES (%s, %s, %s, %s)",
                    (user_id, title, service, content_id.strip()[:100]))

    await in_db(user_id, work)
    return {"recorded": True, "person": username, "title": title}


if __name__ == "__main__":
    if "--http" not in sys.argv:
        sys.exit("taste_mcp.py only runs over HTTP (it needs to know who is asking): python taste_mcp.py --http")
    print(f"taste-mcp starting on http://127.0.0.1:{config.TASTE_MCP_PORT}/mcp "
          f"(tokens from {config.OIDC_ISSUER})", file=sys.stderr)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=config.TASTE_MCP_PORT)