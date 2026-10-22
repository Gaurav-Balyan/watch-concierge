"""
OpenTelemetry tracing (Phase 3, Step 4): the span tree tells the truth about who did what.

Offline part: a scripted model drives the team over the eval's fake servers; spans are
kept in memory and checked: one trace, the Catalog agent nested INSIDE the Supervisor's
tool call, each model call records which tools it was offered, blocked calls are visible.
HTTP part (needs Keycloak + Postgres): no span attribute ever contains a token; tool
results stay out of spans unless TRACE_CONTENT=1.

Run:  python tests/test_tracing.py        (or: pytest tests)
"""
import asyncio
import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import agent
import config
import multi_agent
import tracing
from test_agent_recovery import reply
from test_auth import Skip
from test_multi_agent import FAKE_SERVERS, HAPPY, TASTE_URL, RecordingGroq, servers, temp_login, token_spy

MEMORY = InMemorySpanExporter()
tracing.setup("test", extra_exporter=MEMORY)      # once per process: a global provider


def traced_team(script, request="play The Friend 2025", mapping=None):
    MEMORY.clear()
    fake = RecordingGroq(script)
    saved = agent.AsyncGroq, agent.SERVERS
    agent.AsyncGroq, agent.SERVERS = (lambda: fake), dict(mapping or FAKE_SERVERS)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(multi_agent.run_team(request, []))
    finally:
        agent.AsyncGroq, agent.SERVERS = saved
    return list(MEMORY.get_finished_spans())


def ours(spans):
    """Only our spans (the mcp SDK adds its own 'MCP send ...' spans too)."""
    return [s for s in spans if s.instrumentation_scope.name == "watch_concierge"]


def by_name(spans, name):
    found = [s for s in spans if s.name == name]
    assert found, f"no span {name!r} in {[s.name for s in spans]}"
    return found


def parent_of(spans, s):
    ids = {x.context.span_id: x for x in spans}
    return ids.get(s.parent.span_id) if s.parent else None


# ----------------------------------------------------------------- offline
def test_one_request_is_one_trace():
    spans = traced_team(list(HAPPY))
    assert len({s.context.trace_id for s in spans}) == 1
    roots = [s for s in spans if s.parent is None]
    assert [r.name for r in roots] == ["agent supervisor"], [r.name for r in roots]


def test_specialist_runs_inside_the_supervisors_tool_call():
    spans = ours(traced_team(list(HAPPY)))
    catalog = by_name(spans, "agent catalog")[0]
    bridge = parent_of(spans, catalog)
    assert bridge.name == "execute_tool ask_catalog_agent", bridge.name
    assert parent_of(spans, bridge).name == "agent supervisor"
    search = by_name(spans, "execute_tool search_title")[0]
    assert parent_of(spans, search).name == "agent catalog"         # the search belongs to the specialist
    play = by_name(spans, "execute_tool play_title")[0]
    assert parent_of(spans, play).name == "agent supervisor"        # the play belongs to the Supervisor


def test_each_model_call_records_who_and_which_tools():
    spans = ours(traced_team(list(HAPPY)))
    chats = [s for s in spans if s.name.startswith("chat ")]
    assert len(chats) == 5
    for c in chats:
        who, offered = c.attributes["gen_ai.agent.name"], set(c.attributes["gen_ai.request.tools"])
        if who == "catalog":
            assert offered == {"search_title", "search_youtube"}, offered
        else:
            assert who == "supervisor" and "search_title" not in offered, (who, offered)


def test_blocked_and_refused_calls_are_visible():
    spans = ours(traced_team([
        reply(calls=[("record_watch", '{"title": "x", "service": "netflix", "content_id": "1"}')]),
        reply(text="ok"),
    ]))
    s = by_name(spans, "execute_tool record_watch")[0]
    assert s.attributes["tool.executed"] is False
    assert s.attributes["tool.blocked"] == "agent-only tool"
    assert not s.status.is_ok


def test_agent_span_says_how_it_ended():
    spans = ours(traced_team(list(HAPPY)))
    assert by_name(spans, "agent catalog")[0].attributes["agent.outcome"] == "answered after 2 turns"
    assert by_name(spans, "agent supervisor")[0].attributes["agent.outcome"] == "answered after 3 turns"


# ----------------------------------------------------------------- HTTP (Keycloak, Postgres)
TASTE_READ = [
    reply(calls=[("ask_taste_agent", '{"question": "what do you know about my taste?"}')]),
    reply(calls=[("get_my_taste", "{}")]),
    reply(text="You like things."),
    reply(text="You like things."),
]


def all_attribute_text(spans) -> str:
    return " ".join(json.dumps(dict(s.attributes or {}), default=str) for s in spans)


def test_no_token_ever_lands_in_a_span():
    from test_taste_http import taste_server
    with temp_login("gaurav"), taste_server(), token_spy() as requested:
        spans = traced_team(list(TASTE_READ), "what do you know about my taste?",
                            {**FAKE_SERVERS, "taste": TASTE_URL})
    assert requested, "no token was requested: the test did not exercise HTTP"
    text = all_attribute_text(spans)
    for _, token in requested:
        assert token not in text and token[-40:] not in text
    tok = by_name(ours(spans), "keycloak token")[0]
    assert tok.attributes["keycloak.audience"] == "taste-mcp"        # WHICH token, never the token
    assert by_name(ours(spans), "connect taste")[0].attributes["enduser.id"] == "gaurav"


def test_tool_results_stay_out_unless_asked():
    from test_taste_http import taste_server
    saved = config.TRACE_CONTENT
    try:
        with temp_login("gaurav"), taste_server():
            config.TRACE_CONTENT = False
            off = by_name(ours(traced_team(list(TASTE_READ), "taste?", {**FAKE_SERVERS, "taste": TASTE_URL})),
                          "execute_tool get_my_taste")[0]
            config.TRACE_CONTENT = True
            on = by_name(ours(traced_team(list(TASTE_READ), "taste?", {**FAKE_SERVERS, "taste": TASTE_URL})),
                         "execute_tool get_my_taste")[0]
    finally:
        config.TRACE_CONTENT = saved
    assert "tool.result" not in off.attributes
    assert '"person": "gaurav"' in on.attributes["tool.result"]


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