"""
Household sharing for the Consensus agent (Phase 3, Step 5).

Server side (taste-mcp): reading someone else's taste needs ALL of
  * the permission  : token scope taste-household-read (only the Consensus agent asks for it),
  * membership      : the name exists in household_members,
  * consent         : that person turned sharing on THEMSELVES,
and never returns watch history. Every lookup, allowed or refused, is audited.
Agent side: only the Consensus agent's token carries the permission and only it is offered
get_household_taste; a scripted team run shows Supervisor -> Consensus -> Catalog (agent -> agent).

Needs Keycloak + Postgres (skipped otherwise). Uses household_test, never your real data.
Run:  python tests/test_household.py        (or: pytest tests)
"""
import asyncio
import contextlib
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

from concierge import engine as agent
from concierge import team as multi_agent
from concierge import skill_agents
from test_agent_recovery import reply
from test_auth import Skip, keycloak_token
from test_multi_agent import FAKE_SERVERS, TASTE_URL, run_offline, temp_login, token_spy
from test_taste_http import ADMIN_TEST_DB_URL, TEST_DB_URL, URL, taste_server

RUN = uuid.uuid4().hex[:8]
HOUSEHOLD = "openid taste-mcp taste-household-read"
PLAIN = "openid taste-mcp"


async def _call(username, scope, tool, args=None):
    token = keycloak_token(username, scope)
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(URL, http_client=http)) as client:
        r = await client.call_tool(tool, args or {})
    text = "\n".join(getattr(b, "text", "") or "" for b in r.content)
    return r.is_error, (text if r.is_error else json.loads(text))


def call(username, tool, args=None, scope=PLAIN):
    return asyncio.run(_call(username, scope, tool, args))


def ok(username, tool, args=None, scope=PLAIN):
    is_error, out = call(username, tool, args, scope)
    assert not is_error, out
    return out


def audit_rows():
    with psycopg.connect(ADMIN_TEST_DB_URL) as conn:
        return conn.execute("SELECT asked_by, about, allowed, reason FROM household_reads ORDER BY id").fetchall()


def person(result, name):
    return [p for p in result["people"] if p["name"] == name][0]


@contextlib.contextmanager
def household():
    """A fresh taste server on the test DB; both people known, both with one like, nobody sharing."""
    with taste_server():
        ok("gaurav", "remember_taste", {"kind": "like", "item": f"g-like-{RUN}"})
        ok("mishika", "remember_taste", {"kind": "like", "item": f"m-like-{RUN}"})
        ok("mishika", "remember_taste", {"kind": "dislike", "item": f"m-dislike-{RUN}"})
        yield


# ----------------------------------------------------------------- server: the four rules
def test_without_the_permission_nobody_elses_taste_and_it_is_audited():
    with household():
        before = len(audit_rows())
        is_error, text = call("gaurav", "get_household_taste", {"names": ["mishika"]}, scope=PLAIN)
        assert is_error and "FORBIDDEN" in text, text
        new = audit_rows()[before:]
        assert ("gaurav", "mishika", False, "no permission") in new, new


def test_no_consent_no_taste():
    with household():
        out = ok("gaurav", "get_household_taste", {"names": ["gaurav", "mishika"]}, scope=HOUSEHOLD)
        assert person(out, "gaurav")["included"] and f"g-like-{RUN}" in person(out, "gaurav")["likes"]
        m = person(out, "mishika")
        assert m == {"name": "mishika", "included": False, "reason": "not sharing"}, m


def test_after_consent_likes_and_dislikes_but_never_history():
    with household():
        ok("mishika", "set_household_sharing", {"share": True})
        out = ok("gaurav", "get_household_taste", {"names": ["Mishika"]}, scope=HOUSEHOLD)   # any case
        m = person(out, "mishika")
        assert m["included"] and f"m-like-{RUN}" in m["likes"] and f"m-dislike-{RUN}" in m["dislikes"], m
        assert "recently_watched" not in json.dumps(out) and "watched_at" not in json.dumps(out)
        assert out["never_shared"] == "watch history"


def test_unknown_names_get_nothing():
    with household():
        out = ok("gaurav", "get_household_taste", {"names": [f"stranger-{RUN}"]}, scope=HOUSEHOLD)
        assert person(out, f"stranger-{RUN}") == {"name": f"stranger-{RUN}", "included": False,
                                                 "reason": "not in household"}


def test_consent_can_only_be_changed_for_yourself():
    with household():
        # The tool has no "who" argument; Gaurav switching sharing on changes only Gaurav's row.
        ok("gaurav", "set_household_sharing", {"share": True})
        out = ok("gaurav", "get_household_taste", {"names": ["mishika"]}, scope=HOUSEHOLD)
        assert person(out, "mishika")["included"] is False
        # ...and Mishika turning it off again works.
        ok("mishika", "set_household_sharing", {"share": True})
        ok("mishika", "set_household_sharing", {"share": False})
        out = ok("gaurav", "get_household_taste", {"names": ["mishika"]}, scope=HOUSEHOLD)
        assert person(out, "mishika")["reason"] == "not sharing"


def test_every_lookup_is_audited_and_the_app_cannot_erase_it():
    with household():
        before = len(audit_rows())
        ok("mishika", "set_household_sharing", {"share": True})
        ok("gaurav", "get_household_taste", {"names": ["mishika", f"stranger-{RUN}"]}, scope=HOUSEHOLD)
        new = audit_rows()[before:]
        assert ("gaurav", "mishika", True, "shared") in new, new
        assert ("gaurav", f"stranger-{RUN}", False, "not in household") in new, new
        with psycopg.connect(TEST_DB_URL) as conn:             # as taste_app, the server's own role
            try:
                conn.execute("DELETE FROM household_reads")
            except psycopg.errors.InsufficientPrivilege:
                return
        raise AssertionError("taste_app could delete the audit log")


# ----------------------------------------------------------------- agents
def test_only_the_consensus_agent_holds_the_permission():
    async def open_(a):
        async with agent.McpToolbox(a.servers, a.scopes, a.offer, a.hide) as box:
            for t in a.local_tools:
                box.add_local(t)
            return box.offered

    saved = agent.SERVERS
    agent.SERVERS = {**FAKE_SERVERS, "taste": TASTE_URL}
    try:
        with temp_login("gaurav"), household(), token_spy() as requested:
            taste_offered = asyncio.run(open_(skill_agents.taste_agent()))
            consensus_offered = asyncio.run(open_(skill_agents.consensus_agent()))
    finally:
        agent.SERVERS = saved
    assert [aud for aud, _ in requested] == ["taste-mcp", "taste-mcp+taste-household-read"], requested
    assert "get_household_taste" not in taste_offered and "set_household_sharing" in taste_offered
    # Step 7: titles are checked by check_titles (code, parallel), not by nested Catalog agents.
    assert consensus_offered == {"get_household_taste", "check_titles"}, consensus_offered


def test_supervisor_is_told_who_me_is():
    # Seen live: without it the model wrote "Watching: me, mishika" and Consensus asked for "user".
    saved = agent.SERVERS
    agent.SERVERS = {**FAKE_SERVERS, "taste": TASTE_URL}
    try:
        with temp_login("gaurav"):
            system = multi_agent.supervisor().system
    finally:
        agent.SERVERS = saved
    assert "username is 'gaurav'" in system and "never 'me' or 'user'" in system
    assert "never words like 'me', 'I' or 'user'" in skill_agents.CONSENSUS_SYSTEM
    # Seen live: "suggest a few comedies" had no route; the Supervisor answered with 8 titles from memory.
    assert "for one person or several" in system and "NEVER name a title" in system


def test_taste_agent_cannot_call_the_household_tool_even_if_it_tries():
    script = [
        reply(calls=[("ask_taste_agent", '{"question": "what does mishika like?"}')]),         # supervisor
        reply(calls=[("get_household_taste", '{"names": ["mishika"]}')]),                    # taste (not offered)
        reply(text="I can only see your own taste."),
        reply(text="I can only see your own taste."),
    ]
    with temp_login("gaurav"), household():
        ok("mishika", "set_household_sharing", {"share": True})
        before = len(audit_rows())
        _, trace, _ = run_offline(script, "what does mishika like?", {**FAKE_SERVERS, "taste": TASTE_URL})
        assert len(audit_rows()) == before                     # it never even reached the server
    t = [t for t in trace if t["tool"] == "get_household_taste"][0]
    assert t["agent"] == "taste" and t["executed"] is False and "Unknown tool" in t["result"]


def test_team_supervisor_consensus_check_titles():
    question = "Watching: gaurav, mishika. Request: something we would both like"
    titles = json.dumps({"titles": [{"title": "The Friend", "year": "2025"}, {"title": f"Nope {RUN}"}]})
    script = [
        reply(calls=[("ask_consensus_agent", json.dumps({"question": question}))]),           # supervisor
        reply(calls=[("get_household_taste", '{"names": ["gaurav", "mishika"]}')]),           # consensus
        reply(calls=[("check_titles", titles)]),                                               # consensus
        reply(text="Both included. Confirmed: The Friend (2025), netflix 82182295."),           # consensus
        reply(text="You could watch The Friend (2025) on Netflix. Shall I play it?"),            # supervisor
    ]
    with temp_login("gaurav"), household():
        ok("mishika", "set_household_sharing", {"share": True})
        answer, trace, fake = run_offline(script, "me and Mishika, something we both like",
                                          {**FAKE_SERVERS, "taste": TASTE_URL})
    steps = [(t["agent"], t["tool"]) for t in trace]
    assert steps[0] == ("consensus", "get_household_taste")
    assert sorted(steps[1:3]) == [("check_titles", "search_title")] * 2       # in parallel, by CODE
    assert steps[3:] == [("consensus", "check_titles"), ("supervisor", "ask_consensus_agent")], steps
    checked = json.loads(trace[3]["result"])
    assert [m["title"] for m in checked["confirmed"]] == ["The Friend"] and checked["not_found"] == [f"Nope {RUN}"]
    m = person(json.loads(trace[0]["result"]), "mishika")
    assert m["included"] and f"m-like-{RUN}" in m["likes"]
    assert len(fake.calls) == 5                     # was 7 with nested Catalog agents (2 calls per title)
    assert not any(t["tool"] == "play_title" for t in trace)   # options first; play only when the user picks


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