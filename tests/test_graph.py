"""
The LangGraph concierge (Phase 4, Steps 1-2): the SAME behaviour as routed.py, now as a graph.

    shape     the graph has the nodes and edges we designed (route -> one node per route, see graph.py)
    parity    for PLAY / SONG / STATUS requests the graph gives the SAME answer and the SAME tool calls
              as routed.py, with ZERO model calls (a model that raises if called proves it)
    edges     unclear -> the team; a workflow saying "not mine" -> the team; no taste server -> the team;
              TASTE -> taste; SUGGEST -> suggest; "I like X, suggest ..." -> taste THEN suggest
    spans     one trace: graph -> node route, node play -> the tool calls inside node play
    state     plain data only (Step 2 saves it with a checkpointer)

No Groq key, no Keycloak, no TV. Fake catalog + fake TV from the evals (stdio).
Run:  python tests/test_graph.py        (or: python -m pytest tests)
"""
import asyncio
import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from concierge import engine as agent
from common import config
from concierge import graph
from concierge import skill_agents
from baselines import routed
from concierge import router
from test_agent_recovery import reply
from test_multi_agent import RecordingGroq
from test_routing import FIXED, FRIEND, NoModel, calls
from test_tracing import MEMORY, by_name, ours, parent_of      # one tracer provider per process


def run(request, entry=None, servers=None, model=NoModel):
    entry = entry or graph.run_graph
    saved = agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL
    agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL = model, dict(servers or FIXED), "", ""
    trace = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            answer = asyncio.run(entry(request, trace))
    finally:
        agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL = saved
    return answer, trace


def tools(trace):
    return [(t["tool"], t["args"]) for t in trace]


# ----------------------------------------------------------------- shape
def test_the_graph_has_the_designed_shape():
    g = graph.GRAPH.get_graph()
    assert set(g.nodes) == {"__start__", "route", "play", "song", "status", "taste", "suggest", "fallback",
                            "ask", "choose", "remember", "__end__"}, set(g.nodes)
    edges = {(e.source, e.target) for e in g.edges}
    assert edges == {("__start__", "route"),
                     *(("route", n) for n in ("play", "song", "status", "taste", "suggest", "fallback")),
                     ("play", "fallback"), ("play", "remember"), ("song", "fallback"), ("song", "remember"),
                     ("play", "ask"), ("ask", "choose"), ("choose", "ask"), ("choose", "remember"),   # idea 1
                     ("status", "remember"), ("taste", "suggest"), ("taste", "remember"),
                     ("suggest", "remember"), ("fallback", "remember"),
                     ("remember", "__end__")}, edges                     # idea 1c: every turn ends in remember


# ----------------------------------------------------------------- parity with routed.py
NO_MODEL_CASES = ["play The Friend 2025", "play The Friend", "play Dual Title 2020", "play Dual Title 2020 on prime",
                  "play Game Changer", "play zzzxqq",                                   # PLAY
                  "play tum hi ho by arijit singh", "play the song zzzxqq on youtube",  # SONG
                  "is the tv on?"]                                                      # STATUS


def test_workflow_requests_match_routed_exactly_with_zero_model_calls():
    for request in NO_MODEL_CASES:
        a_graph, t_graph = run(request)
        a_routed, t_routed = run(request, routed.run_routed)
        assert a_graph == a_routed, (request, a_graph, a_routed)
        assert tools(t_graph) == tools(t_routed), (request, tools(t_graph), tools(t_routed))


def test_play_one_match_plays_it_by_code():
    answer, trace = run("play The Friend 2025")
    assert trace[0]["args"]["route"] == "PLAY"
    assert calls(trace, "play_title")[0]["args"]["content_id"] == FRIEND
    assert "simulation" in answer


# ----------------------------------------------------------------- the conditional edges
def test_unclear_request_goes_to_the_team():
    fake = RecordingGroq([reply(text="Sorry, I can't pause the TV.")])
    answer, trace = run("pause the tv", model=lambda: fake)
    assert trace[0]["args"]["route"] == "FALLBACK" and answer == "Sorry, I can't pause the TV."
    assert "ask_catalog_agent" in fake.calls[0]["tools"]                     # it was the team


def test_play_workflow_saying_not_mine_hands_over_to_the_team():
    # A PLAY route with no title: workflows.play_title returns None ("not mine"); after_play must
    # send the request on to the team instead of ending with no answer.
    saved = router.route

    async def empty_title(request):
        return router.Route("PLAY", "test: a PLAY route with no title", "rule", {"title": ""})
    fake = RecordingGroq([reply(text="Which title would you like?")])
    router.route = empty_title
    try:
        answer, trace = run("play", model=lambda: fake)
    finally:
        router.route = saved
    assert answer == "Which title would you like?" and len(fake.calls) == 1, (answer, len(fake.calls))
    assert not calls(trace, "search_title")


class FakeSkillAgent:
    """Stands in for the taste / consensus agent: records what it was asked, in order."""
    def __init__(self, name, log):
        self.name, self.log = name, log

    async def run(self, question, trace, history=None):
        self.log.append((self.name, question))
        return f"answer from {self.name}"


def run_with_taste(request):
    """Taste server 'present' and the skill agents faked: this tests the graph's WIRING (which node
    runs, in which order, whose answer wins). The real agents are tested in test_multi_agent / household."""
    log, saved = [], (skill_agents.has_taste, skill_agents.taste_agent, skill_agents.consensus_agent,
                      skill_agents.logged_in_username)
    skill_agents.has_taste = lambda: True
    skill_agents.taste_agent = lambda: FakeSkillAgent("taste", log)
    skill_agents.consensus_agent = lambda trace=None: FakeSkillAgent("consensus", log)
    skill_agents.logged_in_username = lambda: "gaurav"
    try:
        answer, trace = run(request)                          # NoModel: the router must not need one
    finally:
        (skill_agents.has_taste, skill_agents.taste_agent, skill_agents.consensus_agent,
         skill_agents.logged_in_username) = saved
    return answer, trace, log


def test_taste_goes_to_the_taste_agent_only():
    answer, trace, log = run_with_taste("I don't like horror")
    assert [n for n, _ in log] == ["taste"] and answer == "answer from taste"
    assert log[0][1] == "I don't like horror"


def test_suggest_goes_to_the_consensus_agent_with_the_logged_in_user():
    answer, _, log = run_with_taste("me and Mishika want to watch something tonight")
    assert [n for n, _ in log] == ["consensus"] and answer == "answer from consensus"
    assert log[0][1].startswith("Watching: gaurav,")              # who is told by CODE, from the login


def test_preference_plus_suggestion_saves_the_taste_first_then_suggests():
    answer, _, log = run_with_taste("I like comedy, can you suggest a few titles")
    assert [n for n, _ in log] == ["taste", "consensus"], log    # the ORDER is the edge taste -> suggest
    assert answer == "answer from consensus"                     # the suggestion is the answer


def test_without_a_taste_server_taste_and_suggest_go_to_the_team():
    for request in ("I don't like horror", "me and Mishika want to watch something tonight"):
        fake = RecordingGroq([reply(text="I can't do that right now.")])
        answer, trace = run(request, model=lambda: fake)
        assert trace[0]["args"]["route"] in ("TASTE", "SUGGEST") and len(fake.calls) == 1, request
        assert "ask_catalog_agent" in fake.calls[0]["tools"]       # it was the team


# ----------------------------------------------------------------- spans
def test_nodes_show_as_spans_in_one_trace():
    MEMORY.clear()
    run("play The Friend 2025")
    spans = ours(list(MEMORY.get_finished_spans()))
    assert len({s.context.trace_id for s in spans}) == 1
    root = by_name(spans, "graph")[0]
    assert root.parent is None
    nodes = sorted((s for s in spans if s.name.startswith("node ")), key=lambda s: s.start_time)
    assert [s.name for s in nodes] == ["node route", "node play", "node remember"], [s.name for s in nodes]
    assert all(parent_of(spans, s) is root for s in nodes)
    assert nodes[0].attributes["route.kind"] == "PLAY"
    for tool in ("execute_tool search_title", "execute_tool play_title"):
        assert parent_of(spans, by_name(spans, tool)[0]).name == "node play", tool


# ----------------------------------------------------------------- state
def test_state_is_plain_data():
    # Step 2 saves the state after every node; only JSON-like values survive that.
    saved = agent.SERVERS
    agent.SERVERS = dict(FIXED)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            state = asyncio.run(graph.GRAPH.ainvoke({"request": "play The Friend 2025"}, context=graph.Run()))
    finally:
        agent.SERVERS = saved
    assert set(state) == {"request", "route", "answer", "history", "known_ids"}, set(state)   # + idea 1c
    assert json.loads(json.dumps(state)) == state


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for n, f in tests:
        f()
        print("PASS", n)
    print(f"\nAll {len(tests)} graph tests passed.")