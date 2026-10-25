"""
Supervisor + Catalog agent + Taste agent (Phase 3, Steps 2-3).

Offline part (no Groq key, no Keycloak): a scripted model drives BOTH agents over the
eval's fake catalog and fake TV (stdio). Checks the handoff and that each agent's
model was offered only its own tools and saw only its own conversation.

HTTP part (needs Keycloak; taste tests also need Postgres): the tokens the specialists
ACTUALLY requested are replayed against device-mcp and refused; the Taste agent saves
for the TOKEN's person whatever name the words mention; the code-only recorder writes
history after a real play and opens no connection after a dry run.
Logins go to a temporary file: your own .login.json is never touched.

Run:  python tests/test_multi_agent.py        (or: pytest tests)
"""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from concierge import engine as agent
from common import config
from common import login
from concierge import team as multi_agent
from concierge import recorder, skill_agents
from test_agent_recovery import reply
from test_auth import Skip

FAKE_SERVERS = {"catalog": "evals/srv_catalog_ok.py", "device": "evals/srv_device_dryrun.py"}
FRIEND_ID = "82182295"    # evals/fixtures.py
RUN = uuid.uuid4().hex[:8]   # unique marker for rows written by this test run


class RecordingGroq:
    """Plays one script shared by every agent (they run one after another). Records, per
    model call, which tools were offered and which messages were sent."""
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append({"tools": sorted(t["function"]["name"] for t in kw.get("tools", [])),  # router: none
                           "messages": [dict(m) for m in kw["messages"]]})
        return self.script.pop(0)


def run_offline(script, request="play The Friend 2025", servers=None):
    fake = RecordingGroq(script)
    saved = agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL
    # CATALOG_AGENT_URL "": the in-process Catalog agent, whatever .env says (test_a2a tests the A2A path).
    agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL = (lambda: fake), dict(servers or FAKE_SERVERS), ""
    trace = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            answer = asyncio.run(multi_agent.run_team(request, trace))
    finally:
        agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL = saved
    return answer, trace, fake


HAPPY = [
    reply(calls=[("ask_catalog_agent", '{"question": "Where is The Friend (2025) streaming?"}')]),  # supervisor
    reply(calls=[("search_title", '{"title": "The Friend", "year": "2025"}')]),                    # catalog
    reply(text='{"matches": [{"title": "The Friend", "year": "2025", "ids": {"netflix": "%s"}}]}' % FRIEND_ID),
    reply(calls=[("play_title", '{"service": "netflix", "content_id": "%s", "title": "The Friend"}' % FRIEND_ID)]),
    reply(text="Simulated: The Friend on Netflix."),                                                 # supervisor
]


# ----------------------------------------------------------------- offline
def test_supervisor_hands_over_and_plays():
    answer, trace, _ = run_offline(list(HAPPY))
    steps = [(t["agent"], t["tool"]) for t in trace]
    assert steps == [("catalog", "search_title"), ("supervisor", "ask_catalog_agent"),
                     ("supervisor", "play_title")], steps
    assert FRIEND_ID in trace[1]["result"]          # the Catalog agent's answer came back as a tool result
    assert json.loads(trace[2]["result"])["status"] == "dry_run"
    assert answer.startswith("Simulated")


def test_each_model_is_offered_only_its_own_tools():
    _, _, fake = run_offline(list(HAPPY))
    supervisor_calls = [fake.calls[i] for i in (0, 3, 4)]
    catalog_calls = [fake.calls[i] for i in (1, 2)]
    for c in catalog_calls:
        assert c["tools"] == ["search_title", "search_youtube"], c["tools"]
    for c in supervisor_calls:
        assert "search_title" not in c["tools"] and "ask_catalog_agent" in c["tools"], c["tools"]


def test_catalog_agent_sees_only_the_question():
    # Separate conversations: the Catalog model gets its own prompt + the question, nothing else.
    _, _, fake = run_offline(list(HAPPY))
    first = fake.calls[1]["messages"]
    assert [m["role"] for m in first] == ["system", "user"]
    assert first[0]["content"] == multi_agent.CATALOG_SYSTEM
    assert first[1]["content"] == "Where is The Friend (2025) streaming?"
    assert "play" not in first[1]["content"].lower()


def test_supervisor_cannot_search_itself():
    answer, trace, _ = run_offline([
        reply(calls=[("search_title", '{"title": "The Friend"}')]),
        reply(text="I could not search."),
    ])
    assert trace[0]["agent"] == "supervisor" and trace[0]["executed"] is True
    assert trace[0]["result"] == "Unknown tool: search_title"    # no route: never reached a server


TASTE_URL = f"http://127.0.0.1:{config.TASTE_MCP_PORT}/mcp"
SONG_ID = "Umqb9KENgmk"   # evals/fixtures.py YOUTUBE


def test_a_song_goes_to_youtube():
    answer, trace, fake = run_offline([
        reply(calls=[("ask_catalog_agent", '{"question": "Find on YouTube: tum hi ho arijit singh"}')]),
        reply(calls=[("search_youtube", '{"query": "tum hi ho arijit singh"}')]),                  # catalog
        reply(text='{"youtube": [{"video_id": "%s", "title": "Tum Hi Ho", "channel": "T-Series"}]}' % SONG_ID),
        reply(calls=[("play_title", '{"service": "youtube", "content_id": "%s", "title": "Tum Hi Ho"}' % SONG_ID)]),
        reply(text="Simulated: Tum Hi Ho on YouTube."),
    ], "play tum hi ho by arijit singh on youtube")
    steps = [(t["agent"], t["tool"]) for t in trace]
    assert steps == [("catalog", "search_youtube"), ("supervisor", "ask_catalog_agent"),
                     ("supervisor", "play_title")], steps
    assert json.loads(trace[0]["result"])["videos"][0]["video_id"] == SONG_ID
    played = json.loads(trace[2]["result"])
    assert played["status"] == "dry_run" and played["service"] == "youtube" and played["content_id"] == SONG_ID


def test_a_youtube_link_is_not_a_video_id():
    # device-mcp's OWN check, called by code. (Since Phase 5 a MODEL never gets this far with a link: the
    # agent loop blocks any id no search returned. test_injection.py tests that.)
    async def play_link():
        async with agent.McpToolbox(FAKE_SERVERS) as box:
            return await box.call("play_title", {"service": "youtube", "content_id": "https://youtu.be/%s" % SONG_ID})
    text, is_error = asyncio.run(play_link())
    assert is_error and "INVALID_INPUT" in text, text


def test_eval_runner_scores_a_team_run():
    # The eval runner with --agent team: the SAME case checks score the team's merged trace, and the
    # model calls of BOTH agents are counted (2 Catalog + 3 Supervisor = 5).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
    import run_evals
    from cases import CASES_BY_ID
    fake = RecordingGroq(list(HAPPY))
    saved = agent.AsyncGroq, agent.SERVERS, run_evals.AGENT_KIND
    agent.AsyncGroq, run_evals.AGENT_KIND = (lambda: fake), "team"
    try:
        rec = asyncio.run(run_evals.run_once(CASES_BY_ID[1], 1, retries=0, backoff=0))
    finally:
        agent.AsyncGroq, agent.SERVERS, run_evals.AGENT_KIND = saved
    assert rec["status"] == "pass", {k: v for k, v in rec["checks"].items() if not v["ok"]}
    assert rec["model_calls"] == 5, rec["model_calls"]
    assert {t["agent"] for t in rec["trace"]} == {"catalog", "supervisor"}


def test_supervisor_knows_the_youtube_route():
    saved = agent.SERVERS
    agent.SERVERS = dict(FAKE_SERVERS)
    try:
        system = multi_agent.supervisor().system
    finally:
        agent.SERVERS = saved
    assert "Find on YouTube:" in system and "service 'youtube'" in system
    assert "search_youtube" in multi_agent.CATALOG_SYSTEM
    # Seen live: with an old catalog-mcp (no search_youtube) the Catalog agent made NO tool call
    # and answered "No YouTube results were found": a claim with nothing behind it.
    assert "Never report a search result without calling the tool" in multi_agent.CATALOG_SYSTEM


def test_supervisor_with_taste_has_only_device_and_two_bridges():
    saved = agent.SERVERS
    agent.SERVERS = {**FAKE_SERVERS, "taste": TASTE_URL}
    try:
        sup = multi_agent.supervisor()
        assert set(sup.servers) == {"device"}, sup.servers               # no taste, no catalog server
        assert [t.name for t in sup.local_tools] == ["ask_catalog_agent", "ask_taste_agent", "ask_consensus_agent"]
        assert "ask_taste_agent" in sup.system
        assert set(skill_agents.taste_agent().servers) == {"taste"}
    finally:
        agent.SERVERS = saved


def test_without_taste_there_is_no_taste_bridge():
    saved = agent.SERVERS
    agent.SERVERS = dict(FAKE_SERVERS)
    try:
        sup = multi_agent.supervisor()
        assert [t.name for t in sup.local_tools] == ["ask_catalog_agent"]
        assert "ask_taste_agent" not in sup.system      # never mention a tool that does not exist
        assert "ask_consensus_agent" not in sup.system
    finally:
        agent.SERVERS = saved


# ----------------------------------------------------------------- HTTP (Keycloak, Postgres)
@contextlib.contextmanager
def temp_login(username):
    """Log in through the real Keycloak page, into a temporary file (never your .login.json)."""
    import kc_browser
    from test_login import need_keycloak
    need_keycloak()
    saved = login.STATE_FILE
    login.STATE_FILE = Path(tempfile.mkdtemp()) / ".login.json"
    try:
        login.login(open_browser=kc_browser.browser_for(username))
        yield
    finally:
        login.STATE_FILE = saved


@contextlib.contextmanager
def token_spy():
    """Records (audience, token) for every token the agent code asks for. Extra permissions are
    shown as 'taste-mcp+taste-household-read'."""
    requested, real = [], login.access_token_for

    def spy(audience, extra_scopes=()):
        token = real(audience, extra_scopes)
        requested.append(("+".join((audience, *extra_scopes)), token))
        return token
    login.access_token_for = spy
    try:
        yield requested
    finally:
        login.access_token_for = real


@contextlib.contextmanager
def servers(mapping):
    saved = agent.SERVERS
    agent.SERVERS = mapping
    try:
        yield
    finally:
        agent.SERVERS = saved


def taste_of(username):
    from test_taste_http import call
    return asyncio.run(call(username, "get_my_taste"))


def specialist_token_is_refused_at_device(make_agent, mapping, expected_tools, audience, server_cm):
    from test_device_http import device_server, refused_because

    async def open_toolbox():
        async with agent.McpToolbox(make_agent().servers) as box:
            return set(box.routes)

    with temp_login("gaurav"), servers(mapping), token_spy() as requested, server_cm(), device_server():
        tools = asyncio.run(open_toolbox())
        assert tools == expected_tools, tools
        assert [aud for aud, _ in requested] == [audience], requested
        assert "WRONG_AUDIENCE" in refused_because(requested[0][1])     # the TV server says no


def test_catalog_agents_real_token_is_refused_at_device_mcp():
    from test_catalog_http import catalog_server
    from test_device_http import URL as DEVICE_URL
    specialist_token_is_refused_at_device(
        multi_agent.catalog_agent,
        {"catalog": f"http://127.0.0.1:{config.CATALOG_MCP_PORT}/mcp", "device": DEVICE_URL},
        {"search_title", "search_youtube"}, "catalog-mcp", catalog_server)


def test_taste_agents_real_token_is_refused_at_device_mcp():
    from test_device_http import URL as DEVICE_URL
    from test_taste_http import taste_server
    specialist_token_is_refused_at_device(
        skill_agents.taste_agent, {"taste": TASTE_URL, "device": DEVICE_URL},
        {"get_my_taste", "remember_taste", "forget_taste", "record_watch",          # routes: all server tools,
         "set_household_sharing", "get_household_taste"}, "taste-mcp", taste_server)  # even hidden ones


def test_team_saves_two_preferences_through_the_taste_agent():
    from test_taste_http import taste_server
    words = f"I don't like horror{RUN}, and I love Nolan{RUN} films"
    script = [
        reply(calls=[("ask_taste_agent", json.dumps({"question": words}))]),                    # supervisor
        reply(calls=[("remember_taste", json.dumps({"kind": "dislike", "item": f"horror{RUN}"})),  # taste
                     ("remember_taste", json.dumps({"kind": "like", "item": f"Nolan{RUN}"}))]),
        reply(text=f"Saved: dislike horror{RUN}, like Nolan{RUN}."),                               # taste
        reply(text="Done, I saved both."),                                                           # supervisor
    ]
    with temp_login("gaurav"), taste_server():
        answer, trace, fake = run_offline(script, words, {**FAKE_SERVERS, "taste": TASTE_URL})
        taste = taste_of("gaurav")                 # read while the server is still running
    steps = [(t["agent"], t["tool"]) for t in trace]
    assert steps == [("taste", "remember_taste"), ("taste", "remember_taste"),
                     ("supervisor", "ask_taste_agent")], steps
    assert fake.calls[1]["messages"][1]["content"] == words              # the person's words, unchanged
    # Offered to the Taste MODEL: never get_household_taste (Step 5: only the Consensus agent).
    assert fake.calls[1]["tools"] == ["forget_taste", "get_my_taste", "remember_taste", "set_household_sharing"]
    for i in (0, 3):                                                       # the Supervisor's model calls
        assert not {"get_my_taste", "remember_taste", "forget_taste"} & set(fake.calls[i]["tools"])
    assert f"horror{RUN}" in taste["dislikes"] and f"Nolan{RUN}" in taste["likes"], taste


def test_whose_taste_comes_from_the_token_not_the_words():
    from test_taste_http import taste_server
    item = f"mishika-horror{RUN}"
    words = "Remember that Mishika hates horror"
    script = [
        reply(calls=[("ask_taste_agent", json.dumps({"question": words}))]),
        reply(calls=[("remember_taste", json.dumps({"kind": "dislike", "item": item}))]),
        reply(text="Saved."),
        reply(text="Saved."),
    ]
    with temp_login("gaurav"), taste_server():
        run_offline(script, words, {**FAKE_SERVERS, "taste": TASTE_URL})
        assert item in taste_of("gaurav")["dislikes"]          # Gaurav's login -> Gaurav's row
        assert item not in taste_of("mishika")["dislikes"]


def test_recorder_writes_history_only_after_a_real_play():
    from test_taste_http import taste_server
    real_title, dry_title = f"Real {RUN}", f"Dry {RUN}"
    args = {"service": "netflix", "content_id": FRIEND_ID}
    with temp_login("gaurav"), taste_server(), servers({**FAKE_SERVERS, "taste": TASTE_URL}), \
            token_spy() as requested:
        asyncio.run(recorder.record_watch_after_play(
            None, "play_title", {**args, "title": dry_title}, json.dumps({"status": "dry_run"})))
        assert requested == []                                  # a dry run opens no connection at all
        asyncio.run(recorder.record_watch_after_play(
            None, "play_title", {**args, "title": real_title}, json.dumps({"status": "requested"})))
        assert [aud for aud, _ in requested] == ["taste-mcp"]
        watched = [h["title"] for h in taste_of("gaurav")["recently_watched"]]
    assert real_title in watched and dry_title not in watched, watched


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