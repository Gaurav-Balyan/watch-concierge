"""
taste-mcp: Mishika cannot read Gaurav's history (Phase 2, Step 8).

Two layers are tested separately, because each must hold on its own:
  * the SERVER takes the user only from the token (no user argument exists),
  * the DATABASE (row-level security) hides other people's rows even from a
    query with no WHERE clause, and refuses to write a row for someone else.

Uses the separate database household_test, never your real data.
Needs Keycloak and the household-db Postgres container (skipped otherwise).

Run:  python tests/test_taste_http.py        (or: pytest tests)
"""
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from common import config
from test_auth import GAURAV, MISHIKA, Skip, keycloak_token
from test_catalog_http import http_server, last_auth_log_line, post_initialize

URL = f"http://127.0.0.1:{config.TASTE_MCP_PORT}/mcp"
SERVER_LOG = config.BASE_DIR / "logs" / "test_taste_http.log"
TEST_DB_URL = config.TASTE_DB_URL.rsplit("/", 1)[0] + "/household_test"
RUN = uuid.uuid4().hex[:8]   # unique marker: rows from this test run


def need_db():
    try:
        psycopg.connect(TEST_DB_URL, connect_timeout=3).close()
    except psycopg.OperationalError as e:
        raise Skip(f"Postgres test database not reachable ({str(e).strip().splitlines()[0]})")


# The superuser, for test SETUP only (the server itself always connects as taste_app).
ADMIN_TEST_DB_URL = "postgresql://postgres:postgres-dev-pass@127.0.0.1:5433/household_test"


def reset_test_taste():
    """Empty taste_items in the TEST database, so old runs never fill a person's 50-item limit.
    watch_history is left alone: it has no limit, and nothing may delete it (by design)."""
    assert ADMIN_TEST_DB_URL.endswith("/household_test"), "refusing to reset anything but the test database"
    try:
        with psycopg.connect(ADMIN_TEST_DB_URL, connect_timeout=3) as conn:
            conn.execute("DELETE FROM taste_items")
            conn.execute("UPDATE household_members SET share_taste = false")   # Phase 3: nobody shares
    except psycopg.OperationalError as e:
        raise Skip(f"Postgres test database not reachable as admin ({str(e).strip().splitlines()[0]})")


def taste_server():
    need_db()
    reset_test_taste()
    return http_server("servers.taste.server", config.TASTE_MCP_PORT, SERVER_LOG, {"TASTE_DB_URL": TEST_DB_URL})


async def call(username: str, tool: str, args: dict | None = None, raw: bool = False):
    token = keycloak_token(username, "openid taste-mcp")
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(URL, http_client=http)) as client:
        result = await client.call_tool(tool, args or {})
    text = "\n".join(getattr(b, "text", "") or "" for b in result.content)
    if raw:
        return result.is_error, text
    assert not result.is_error, text
    return json.loads(text)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- the door
def test_tokens_for_other_servers_are_refused():
    for scope in ("openid catalog-mcp", "openid device-mcp"):
        assert post_initialize(keycloak_token("gaurav", scope), URL)[0] == 401
        assert "WRONG_AUDIENCE" in last_auth_log_line(SERVER_LOG)


def test_tools_have_no_user_argument():
    # The model is never even OFFERED a way to name a person.
    async def schemas():
        token = keycloak_token("gaurav", "openid taste-mcp")
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with http, Client(streamable_http_client(URL, http_client=http)) as client:
            return {t.name: t.input_schema for t in (await client.list_tools()).tools}
    for name, schema in run(schemas()).items():
        args = set(schema.get("properties", {}))
        assert not args & {"user", "user_id", "username", "person", "sub"}, (name, args)


# ----------------------------------------------------------------- server layer
def test_each_person_sees_only_their_own_taste_and_history():
    g_like, g_title = f"gaurav-like-{RUN}", f"Gaurav Movie {RUN}"
    m_like = f"mishika-like-{RUN}"
    run(call("gaurav", "remember_taste", {"kind": "like", "item": g_like}))
    run(call("gaurav", "record_watch", {"title": g_title, "service": "netflix", "content_id": "1"}))
    run(call("mishika", "remember_taste", {"kind": "like", "item": m_like}))

    gaurav = run(call("gaurav", "get_my_taste"))
    mishika = run(call("mishika", "get_my_taste"))
    assert gaurav["person"] == "gaurav" and mishika["person"] == "mishika"
    assert g_like in gaurav["likes"] and g_title in [h["title"] for h in gaurav["recently_watched"]]
    # THE test: nothing of Gaurav's reaches Mishika, and the reverse.
    assert g_like not in json.dumps(mishika) and g_title not in json.dumps(mishika), mishika
    assert m_like not in json.dumps(gaurav), gaurav


def test_naming_gaurav_in_the_arguments_does_not_help_mishika():
    # What a prompt-injected model might try: add a user argument the tool does not have.
    g_like = f"gaurav-secret-{RUN}"
    run(call("gaurav", "remember_taste", {"kind": "like", "item": g_like}))
    is_error, text = run(call("mishika", "get_my_taste",
                              {"user_id": GAURAV, "user": "gaurav", "username": "gaurav"}, raw=True))
    assert g_like not in text, text                       # whatever happened, no leak
    if not is_error:
        assert json.loads(text)["person"] == "mishika", text


def test_liking_replaces_disliking():
    item = f"thrillers-{RUN}"
    run(call("mishika", "remember_taste", {"kind": "dislike", "item": item}))
    run(call("mishika", "remember_taste", {"kind": "like", "item": item}))
    taste = run(call("mishika", "get_my_taste"))
    assert item in taste["likes"] and item not in taste["dislikes"], taste


def test_forget_only_touches_your_own_items():
    shared = f"comedy-{RUN}"   # both people like the same thing
    run(call("gaurav", "remember_taste", {"kind": "like", "item": shared}))
    run(call("mishika", "remember_taste", {"kind": "like", "item": shared}))
    assert run(call("mishika", "forget_taste", {"kind": "like", "item": shared}))["removed"] == 1
    assert shared in run(call("gaurav", "get_my_taste"))["likes"]          # Gaurav's is still there
    assert shared not in run(call("mishika", "get_my_taste"))["likes"]


def test_bad_input_is_rejected_with_a_reason():
    is_error, text = run(call("gaurav", "remember_taste", {"kind": "like", "item": "x" * 101}, raw=True))
    assert is_error and "INVALID_INPUT" in text, text


# ----------------------------------------------------------------- database layer
def db_as(user_id: str | None):
    """A transaction as taste_app, with app.user_id set (or not), like the server does."""
    conn = psycopg.connect(TEST_DB_URL)
    cur = conn.cursor()
    if user_id is not None:
        cur.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
    return conn, cur


def test_database_hides_other_rows_even_without_where():
    run(call("gaurav", "remember_taste", {"kind": "like", "item": f"db-check-{RUN}"}))
    conn, cur = db_as(MISHIKA)
    with conn:
        cur.execute("SELECT DISTINCT user_id::text FROM taste_items")   # a "buggy" query: no WHERE
        owners = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT count(*) FROM watch_history WHERE user_id = %s", (GAURAV,))  # asking directly
        gaurav_rows = cur.fetchone()[0]
    assert owners <= {MISHIKA}, owners
    assert gaurav_rows == 0


def test_database_shows_nothing_when_no_user_is_set():
    conn, cur = db_as(None)
    with conn:
        cur.execute("SELECT count(*) FROM taste_items")
        assert cur.fetchone()[0] == 0


def test_database_refuses_to_write_a_row_for_someone_else():
    conn, cur = db_as(MISHIKA)
    try:
        with conn:
            cur.execute("INSERT INTO taste_items (user_id, kind, item) VALUES (%s, 'like', %s)",
                        (GAURAV, f"planted-{RUN}"))
    except psycopg.errors.InsufficientPrivilege as e:
        assert "row-level security" in str(e), e
        return
    raise AssertionError("Mishika's session wrote a row into Gaurav's data")


def test_app_role_cannot_delete_history_or_drop_tables():
    for sql in ("DELETE FROM watch_history", "DROP TABLE taste_items"):
        conn, cur = db_as(MISHIKA)
        try:
            with conn:
                cur.execute(sql)
        except psycopg.errors.InsufficientPrivilege:
            continue
        raise AssertionError(f"taste_app was allowed to run: {sql}")


# ----------------------------------------------------------------- stdio
def test_without_a_token_there_is_no_user():
    from servers.taste import server as taste_mcp

    async def go():
        async with Client(taste_mcp.mcp) as client:      # in-process, no HTTP, no token
            r = await client.call_tool("get_my_taste", {})
            return r.is_error, "\n".join(getattr(b, "text", "") or "" for b in r.content)
    is_error, text = run(go())
    assert is_error and "NOT_LOGGED_IN" in text, text


# ----------------------------------------------------------------- agent
def test_agent_records_only_real_plays_and_hides_record_watch_from_the_model():
    import tempfile
    from concierge import engine as agent
    import kc_browser
    from common import login
    login.STATE_FILE = Path(tempfile.mkdtemp()) / ".login.json"   # never your real login
    login.login(open_browser=kc_browser.browser_for("mishika"))
    real, dry = f"Played {RUN}", f"Dry {RUN}"

    async def go():
        async with agent.McpToolbox({"taste": URL}) as toolbox:
            offered = [t["function"]["name"] for t in toolbox.tools]
            for title, status in ((real, "requested"), (dry, "dry_run")):
                await agent.record_watch_if_played(
                    toolbox, {"title": title, "service": "netflix", "content_id": "82182295"},
                    json.dumps({"status": status}))
            return offered
    offered = run(go())
    assert "record_watch" not in offered and "get_my_taste" in offered, offered
    watched = [h["title"] for h in run(call("mishika", "get_my_taste"))["recently_watched"]]
    assert real in watched and dry not in watched, watched


# ----------------------------------------------------------------- runners
try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _server():
        with taste_server():
            yield
except ImportError:
    pass

if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    passed = failed = 0
    try:
        with taste_server():
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