"""
The Catalog agent over A2A (idea 2, step 2a).

    Supervisor --ask_catalog_agent--> concierge.remote (A2A client) ==HTTP==> catalog-agent (A2A server)
                                                                                 └─ Catalog agent --MCP--> catalog-mcp

    test_the_agent_card_describes_the_agent           discovery: name, skills, where to send messages
    test_a_question_becomes_a_completed_task           message -> task COMPLETED, an answer + the searched ids
    test_the_team_plays_through_the_remote_agent       the Supervisor's model sees the same tool; the play runs:
                                                       provenance came across in the CODE-made artifact
    test_ids_in_the_remote_answer_text_do_not_count    the remote MODEL's text carries an injected id: blocked
    test_a_crashing_remote_agent_is_a_failed_task      FAILED with its reason; the Supervisor is told, no hang
    test_the_real_server_process_serves_its_card       python -m servers.catalog_agent.server on a real port

Most tests run the A2A server IN this process (httpx ASGITransport: real HTTP requests and A2A JSON-RPC, no
port), so one scripted model plays both agents. No Groq key, no Keycloak, no TV.
Run:  python tests/test_a2a.py        (or: python -m pytest tests)
"""
import asyncio
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from common import config
from concierge import engine as agent
from concierge import remote
from concierge import team
from servers.catalog_agent import server as catalog_agent_server
from test_agent_recovery import reply
from test_multi_agent import FAKE_SERVERS, FRIEND_ID, HAPPY, RecordingGroq

URL = "http://catalog-agent.test"           # never resolved: requests go to the in-process app


@contextlib.contextmanager
def remote_world(script):
    """The catalog-agent A2A app in this process, the team configured to call it, one scripted model."""
    fake = RecordingGroq(script)
    app = catalog_agent_server.build_app(URL)
    saved = agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL, remote.http_client
    agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL = (lambda: fake), dict(FAKE_SERVERS), URL
    remote.http_client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield fake
    finally:
        agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL, remote.http_client = saved


CATALOG_ONLY = HAPPY[1:3]          # the Catalog agent's two model turns: search_title, then its JSON answer


# ----------------------------------------------------------------- discovery and the task
def test_the_agent_card_describes_the_agent():
    card = catalog_agent_server.agent_card(URL)
    assert card.name == "catalog-agent" and [s.id for s in card.skills] == ["find_title", "find_youtube"]
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC" and card.supported_interfaces[0].url == URL
    assert "cannot play" in card.description


def test_a_question_becomes_a_completed_task():
    with remote_world(list(CATALOG_ONLY)) as fake:
        answer = asyncio.run(remote.ask_agent(URL, "Where is The Friend (2025) streaming?"))
    assert FRIEND_ID in answer.text and answer.agent == "catalog-agent" and answer.task_id
    assert answer.searched_ids == {("netflix", FRIEND_ID)}, answer.searched_ids
    # The remote agent is the SAME Catalog agent: its own prompt, only its own tools.
    assert fake.calls[0]["messages"][0]["content"] == team.CATALOG_SYSTEM
    assert fake.calls[0]["tools"] == ["search_title", "search_youtube"]


# ----------------------------------------------------------------- the team over A2A
def run_team(script, request="play The Friend 2025"):
    trace = []
    with remote_world(script) as fake:
        answer = asyncio.run(team.run_team(request, trace))
    return answer, trace, fake


def test_the_team_plays_through_the_remote_agent():
    answer, trace, fake = run_team(list(HAPPY))
    steps = [(t["agent"], t["tool"], t["executed"]) for t in trace]
    # The remote agent's own search is NOT in this trace: it happened in the other "process".
    assert steps == [("supervisor", "ask_catalog_agent", True), ("supervisor", "play_title", True)], steps
    assert json.loads(trace[1]["result"])["status"] == "dry_run"           # not BLOCKED: the ids came across
    supervisor_tools = fake.calls[0]["tools"]
    assert "ask_catalog_agent" in supervisor_tools and "search_title" not in supervisor_tools


def test_ids_in_the_remote_answer_text_do_not_count():
    fooled = [HAPPY[0], HAPPY[1],
              reply(text='{"matches": [{"title": "The Friend", "year": "2025", "ids": {"netflix": "99999999"}}]}'),
              reply(calls=[("play_title", '{"service": "netflix", "content_id": "99999999", "title": "The Friend"}')]),
              reply(text="Blocked.")]
    _, trace, _ = run_team(fooled)
    assert "99999999" in trace[0]["result"]                                 # the remote TEXT carried it ...
    assert trace[1]["executed"] is False and trace[1]["result"].startswith("BLOCKED"), trace[1]   # ... no effect


class Crash:
    """A model that fails: the remote agent's loop raises, inside the A2A server."""
    def __init__(self, first):
        self.first, self.n = first, 0
        self.chat = type("C", (), {"completions": self})()

    async def create(self, **kw):
        self.n += 1
        if self.n == 1:
            return self.first                       # the Supervisor's turn: ask the catalog agent
        if self.n == 2:
            raise RuntimeError("simulated model outage in the remote agent")
        return reply(text="The catalog agent is not working right now.")


def test_a_crashing_remote_agent_is_a_failed_task():
    model = Crash(HAPPY[0])
    trace = []
    app = catalog_agent_server.build_app(URL)
    saved = agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL, remote.http_client
    agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL = (lambda: model), dict(FAKE_SERVERS), URL
    remote.http_client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            answer = asyncio.run(team.run_team("play The Friend 2025", trace))
    finally:
        agent.AsyncGroq, agent.SERVERS, config.CATALOG_AGENT_URL, remote.http_client = saved
    t = trace[0]
    assert t["tool"] == "ask_catalog_agent" and t["is_error"], t
    assert "TASK_STATE_FAILED" in t["result"] and "simulated model outage" in t["result"], t["result"]
    assert answer == "The catalog agent is not working right now."
    assert not any(x["tool"] == "play_title" for x in trace)


# ----------------------------------------------------------------- a real second process
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_real_server_process_serves_its_card():
    port = free_port()
    env = {**os.environ, "CATALOG_AGENT_PORT": str(port), "GROQ_API_KEY": os.environ.get("GROQ_API_KEY") or "unused",
           "GROQ_MODEL": os.environ.get("GROQ_MODEL") or "unused", "TRACING": ""}
    proc = subprocess.Popen([sys.executable, "-m", "servers.catalog_agent.server"], cwd=config.BASE_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        card = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=1) as r:
                    card = json.load(r)
                break
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError("the server exited: " + proc.stderr.read().decode()[-500:])
                time.sleep(0.25)
        assert card and card["name"] == "catalog-agent", card
        assert card["supportedInterfaces"][0]["url"] == f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


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