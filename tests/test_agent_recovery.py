"""
The agent survives a tool call that GROQ rejects (seen live: "year": null for search_title).

A fake model replays what Groq really did: first a 400 tool_use_failed, then a
corrected call, then an answer. No Groq key, no servers, no TV needed.

Run:  python tests/test_agent_recovery.py        (or: pytest tests)
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from groq import BadRequestError

import agent

REAL_400 = {"error": {  # the body Groq returned in the live run, copied as is
    "message": "Tool call validation failed: tool call validation failed: parameters for tool search_title "
               "did not match schema: errors: [`/year`: expected string, but got null]",
    "type": "invalid_request_error", "code": "tool_use_failed",
    "failed_generation": '{"name": "search_title", "arguments": {\n  "title": "The Grand Seduction",\n  "year": null\n}}'}}


def bad_request(body: dict) -> BadRequestError:
    response = httpx.Response(400, request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"))
    return BadRequestError(f"Error code: 400 - {body}", response=response, body=body)


def reply(text=None, calls=()):
    tool_calls = [SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=n, arguments=a))
                  for i, (n, a) in enumerate(calls)]
    message = SimpleNamespace(content=text, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message,
                                                    finish_reason="tool_calls" if calls else "stop")])


class FakeGroq:
    """Plays a script: each item is a reply, or an exception to raise. Records what it was sent."""
    def __init__(self, script):
        self.script, self.seen = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.seen.append([dict(m) for m in kwargs["messages"]])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_with(script):
    fake = FakeGroq(script)
    saved = agent.AsyncGroq, agent.SERVERS
    agent.AsyncGroq = lambda: fake
    agent.SERVERS = {}          # no MCP servers: the unknown-tool path answers instead
    trace = []
    try:
        answer = asyncio.run(agent.run_agent("The Grand Seduction", trace=trace))
    finally:
        agent.AsyncGroq, agent.SERVERS = saved
    return answer, trace, fake


# ----------------------------------------------------------------- tests
def test_rejected_call_is_explained_to_the_model_and_the_run_continues():
    answer, trace, fake = run_with([
        bad_request(REAL_400),
        reply(calls=[("search_title", '{"title": "The Grand Seduction"}')]),   # corrected: no year
        reply(text="Found it."),
    ])
    assert answer == "Found it."
    note = fake.seen[1][-1]["content"]          # what the model saw before its second try
    assert "rejected and NOT run" in note and "expected string, but got null" in note and "never send null" in note
    assert trace[0]["tool"] == "(rejected by provider)" and trace[0]["executed"] is False
    assert trace[1]["tool"] == "search_title"


def test_it_still_stops_at_max_turns():
    answer, trace, _ = run_with([bad_request(REAL_400)] * agent.MAX_TURNS)
    assert answer.startswith("Stopped after"), answer
    assert len(trace) == agent.MAX_TURNS


def test_other_bad_requests_are_not_swallowed():
    other = {"error": {"message": "context too long", "type": "invalid_request_error", "code": "context_length_exceeded"}}
    try:
        run_with([bad_request(other)])
    except BaseException as e:  # noqa: BLE001
        assert isinstance(agent.first_cause(e), BadRequestError)
        return
    raise AssertionError("a non-tool error was swallowed")


def test_first_cause_unwraps_exception_groups():
    inner = ValueError("real one")
    assert agent.first_cause(ExceptionGroup("outer", [ExceptionGroup("mid", [inner])])) is inner


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