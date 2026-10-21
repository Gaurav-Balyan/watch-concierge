"""
The generic Agent loop (Phase 3, Step 1) keeps the concierge's bookkeeping wired in.

record_watch is AGENT ONLY: agent CODE calls it after a real play, the model never can.
Since Step 1 that wiring is the concierge's `after_tool` hook. Without this file, nothing
noticed if the hook was dropped (checked: removing it left every other test green).

A fake model and a fake toolbox: no Groq key, no servers, no TV needed.
Run:  python tests/test_agent_loop.py        (or: pytest tests)
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent
from test_agent_recovery import FakeGroq, reply

PLAY = ("play_title", '{"service": "netflix", "content_id": "81234567", "title": "The Friend"}')


class FakeToolbox:
    """Looks like McpToolbox to the loop. Records every call that reaches a 'server'."""
    def __init__(self, play_status: str, play_fails: bool = False):
        self.play_status = play_status
        self.play_fails = play_fails
        self.calls = []
        self.tools = []                                   # what the model is offered (unused by FakeGroq)
        self.routes = {"play_title": ("device", None), "record_watch": ("taste", None)}
        self.read_only = {"play_title": False, "record_watch": False}

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "play_title" and self.play_fails:
            return json.dumps({"status": "requested", "error": "TV_UNREACHABLE"}), True
        if name == "play_title":
            return json.dumps({"status": self.play_status, "title": args.get("title")}), False
        return json.dumps({"recorded": True}), False


def run(a: agent.Agent, toolbox: FakeToolbox, script):
    saved = agent.AsyncGroq
    agent.AsyncGroq = lambda: FakeGroq(script)
    trace = []
    try:
        answer = asyncio.run(a.loop(toolbox, "play The Friend", trace))
    finally:
        agent.AsyncGroq = saved
    return answer, trace


# ----------------------------------------------------------------- tests
def test_concierge_records_a_real_play_by_code():
    box = FakeToolbox("requested")
    run(agent.concierge(), box, [reply(calls=[PLAY]), reply(text="Sent.")])
    names = [n for n, _ in box.calls]
    assert names == ["play_title", "record_watch"], names
    assert box.calls[1][1] == {"title": "The Friend", "service": "netflix", "content_id": "81234567"}


def test_concierge_does_not_record_a_dry_run():
    box = FakeToolbox("dry_run")
    run(agent.concierge(), box, [reply(calls=[PLAY]), reply(text="Simulated.")])
    assert [n for n, _ in box.calls] == ["play_title"]


def test_concierge_does_not_record_a_failed_play():
    # An error result must never reach the hook, even if its body looks like a real play.
    box = FakeToolbox("requested", play_fails=True)
    run(agent.concierge(), box, [reply(calls=[PLAY]), reply(text="The TV is off.")])
    assert [n for n, _ in box.calls] == ["play_title"]


def test_model_cannot_call_record_watch_itself():
    box = FakeToolbox("requested")
    fake_watch = ("record_watch", '{"title": "Anything", "service": "netflix", "content_id": "1"}')
    _, trace = run(agent.concierge(), box, [reply(calls=[fake_watch]), reply(text="Done.")])
    assert box.calls == [], box.calls                     # never reached a server
    assert trace[0]["executed"] is False and "Unknown tool" in trace[0]["result"]


def test_an_agent_without_the_hook_records_nothing():
    # A specialist that is not the concierge must not do the concierge's bookkeeping.
    box = FakeToolbox("requested")
    plain = agent.Agent("plain", "test", servers={})
    run(plain, box, [reply(calls=[PLAY]), reply(text="Sent.")])
    assert [n for n, _ in box.calls] == ["play_title"]


def test_trace_says_which_agent_made_each_call():
    box = FakeToolbox("dry_run")
    _, trace = run(agent.Agent("catalog", "test", servers={}), box, [reply(calls=[PLAY]), reply(text="ok")])
    assert [t["agent"] for t in trace] == ["catalog"]


def test_concierge_reads_settings_at_call_time():
    # The evals swap agent.SERVERS before calling run_agent; the concierge must see the swap.
    saved = agent.SERVERS, agent.SYSTEM
    agent.SERVERS, agent.SYSTEM = {"x": "y.py"}, "swapped"
    try:
        c = agent.concierge()
        assert c.servers == {"x": "y.py"} and c.system == "swapped"
    finally:
        agent.SERVERS, agent.SYSTEM = saved


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {name}  {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)