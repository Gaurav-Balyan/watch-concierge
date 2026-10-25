"""
Phase 4 resilience: each pattern, and a FAILURE DRILL that proves it (break things on purpose).

    checkpoint   a request that dies midway is resumed from its last saved node: the work already
                 done (the taste saved) is NOT done again
    crash drill  a REAL process kill (os._exit) after the TV command but before the checkpoint; the
                 resume runs the play node again, and the idempotency key stops a second launch
    idempotency  the key is hidden from the model; same request = first result back; a different
                 title in the same request is refused; a NEW request may play the same title again
    retry/breaker/cache, fast TV check, token refresh: further below

No Groq key, no Keycloak, no TV. Run:  python tests/test_resilience.py   (or: python -m pytest tests)
"""
import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from concierge import engine as agent
from common import config
from concierge import graph
from concierge import skill_agents
from common import resilience
from eval_servers import EVAL_STATE
from test_graph import FakeSkillAgent
from test_routing import FRIEND

ROOT = Path(__file__).resolve().parent.parent
LIVE = {"catalog": "evals/srv_catalog_ok.py", "device": "evals/srv_device_live.py"}
LAUNCHES = EVAL_STATE / "launches.log"


def launches() -> list[str]:
    return LAUNCHES.read_text(encoding="utf-8").split() if LAUNCHES.exists() else []


def rid(name: str) -> str:
    """A request id unique to THIS test run: the fake TV's records live in a shared folder (EVAL_STATE),
    so a fixed id would meet the records of an earlier run (seen: run 2 found run 1's keys)."""
    return f"{name}-{os.getpid()}-{os.urandom(3).hex()}"


def fresh_state() -> Path:
    LAUNCHES.unlink(missing_ok=True)
    return Path(tempfile.mkdtemp(prefix="wc_drill_"))


def drill(state: Path, command: str, request: str, request_id: str, crash: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "CHAOS_CRASH": crash, "TRACING": ""}
    return subprocess.run([sys.executable, str(ROOT / "tests" / "drill_driver.py"), str(state), command, request,
                           request_id], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)


# ----------------------------------------------------------------- checkpoint + resume (in-process)
def test_a_crash_in_suggest_resumes_without_saving_the_taste_twice():
    log, calls = [], {"consensus": 0}

    class CrashOnce(FakeSkillAgent):
        async def run(self, question, trace, history=None):      # history: idea 1c
            calls["consensus"] += 1
            if calls["consensus"] == 1:
                raise RuntimeError("simulated crash inside the suggest node")
            return await super().run(question, trace, history)

    saved = (skill_agents.has_taste, skill_agents.taste_agent, skill_agents.consensus_agent,
             skill_agents.logged_in_username, config.STATE_DIR)
    skill_agents.has_taste = lambda: True
    skill_agents.taste_agent = lambda: FakeSkillAgent("taste", log)
    skill_agents.consensus_agent = lambda trace=None: CrashOnce("consensus", log)
    skill_agents.logged_in_username = lambda: "gaurav"
    config.STATE_DIR = fresh_state()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                asyncio.run(graph.run_durable("I like comedy, can you suggest a few titles", "t-suggest"))
                raise AssertionError("the first run should have crashed")
            except RuntimeError:
                pass
            pending = asyncio.run(graph.pending())
            answer = asyncio.run(graph.run_durable(None, "t-suggest"))        # RESUME
            again = asyncio.run(graph.run_durable(None, "t-suggest"))         # finished: nothing runs
    finally:
        (skill_agents.has_taste, skill_agents.taste_agent, skill_agents.consensus_agent,
         skill_agents.logged_in_username, config.STATE_DIR) = saved
    assert [p["id"] for p in pending] == ["t-suggest"] and pending[0]["next"] == ["suggest"], pending
    assert [n for n, _ in log] == ["taste", "consensus"], log          # taste ran ONCE, before the crash
    assert answer == again == "answer from consensus" and calls["consensus"] == 2


# ----------------------------------------------------------------- the crash drill (a real process kill)
def test_crash_after_play_then_resume_launches_once():
    state, request_id = fresh_state(), rid("drill")
    first = drill(state, "run", "play The Friend 2025", request_id, crash="play:after")
    assert first.returncode == 70, (first.returncode, first.stdout[-500:], first.stderr[-500:])
    assert "[chaos]" in first.stdout and "ANSWER" not in first.stdout
    assert launches() == [FRIEND]                         # the TV command WAS sent before the crash

    resumed = drill(state, "resume", "-", request_id)
    assert resumed.returncode == 0, resumed.stderr[-800:]
    assert f"resuming {request_id}" in resumed.stdout and "next: play" in resumed.stdout
    assert "not sent a second time" in resumed.stdout, resumed.stdout[-500:]
    assert launches() == [FRIEND], launches()             # STILL one launch: the key was recognised
    shutil.rmtree(state, ignore_errors=True)


def test_a_new_request_may_play_the_same_title_again():
    state = fresh_state()
    for request_id in (rid("drill-a"), rid("drill-b")):    # two separate requests = two keys
        r = drill(state, "run", "play The Friend 2025", request_id)
        assert r.returncode == 0 and "I sent the commands" in r.stdout, r.stdout[-400:]
    assert launches() == [FRIEND, FRIEND]
    shutil.rmtree(state, ignore_errors=True)


# ----------------------------------------------------------------- idempotency details
async def _toolbox_calls(request_id, calls):
    token = agent.REQUEST_ID.set(request_id)
    try:
        async with agent.McpToolbox(LIVE) as box:
            shown = next(t for t in box.tools if t["function"]["name"] == "play_title")
            out = [await box.call("play_title", a) for a in calls]
    finally:
        agent.REQUEST_ID.reset(token)
    return shown, out


def test_the_model_never_sees_the_key_and_repeats_get_the_first_result():
    LAUNCHES.unlink(missing_ok=True)
    play = {"service": "netflix", "content_id": FRIEND, "title": "The Friend (2025)"}
    other = {"service": "netflix", "content_id": "80100172", "title": "Other"}
    shown, out = asyncio.run(_toolbox_calls(rid("unit"), [play, play, other]))
    assert "idempotency_key" not in json.dumps(shown)                       # hidden from the model
    (t1, e1), (t2, e2), (t3, e3) = out
    assert not e1 and not e2 and json.loads(t2)["duplicate"] is True, t2
    assert json.loads(t1)["status"] == json.loads(t2)["status"] == "requested"
    assert e3 and "ALREADY_PLAYED" in t3, t3                                # one title per request
    assert launches() == [FRIEND]


def test_store_unknown_outcome_is_never_repeated_and_a_sure_failure_may_retry():
    s = resilience.IdempotencyStore(Path(tempfile.mkdtemp()) / "a.sqlite")
    what = {"service": "netflix", "content_id": "1"}
    assert s.begin("k1", what)[0] == "new"
    assert s.begin("k1", what)[0] == "started"           # crashed mid-action: outcome unknown
    s.forget("k1")                                        # e.g. TV_UNREACHABLE: certainly not launched
    assert s.begin("k1", what)[0] == "new"
    s.finish("k1", {"status": "requested"})
    assert s.begin("k1", what) == ("done", what, {"status": "requested"})


# ----------------------------------------------------------------- retry with backoff
def test_retry_backs_off_with_jitter_and_only_for_transient_errors():
    waits, calls = [], [0]

    async def fake_sleep(d):
        waits.append(d)

    async def flaky():
        calls[0] += 1
        if calls[0] < 4:
            raise TimeoutError("slow")
        return "ok"
    assert asyncio.run(resilience.retry_async(flaky, attempts=4, base=1.0, sleep=fake_sleep,
                                              retry_if=lambda e: isinstance(e, TimeoutError))) == "ok"
    assert calls[0] == 4 and len(waits) == 3
    # each wait is 50-100% of 1, 2, 4 s: it grows (backoff) and is random within its band (jitter)
    assert 0.5 <= waits[0] <= 1.0 and 1.0 <= waits[1] <= 2.0 and 2.0 <= waits[2] <= 4.0, waits

    calls[0], waits[:] = 0, []

    async def bad_key():
        calls[0] += 1
        raise PermissionError("401")
    try:
        asyncio.run(resilience.retry_async(bad_key, attempts=3, base=1.0, sleep=fake_sleep,
                                           retry_if=lambda e: isinstance(e, TimeoutError)))
    except PermissionError:
        pass
    assert calls[0] == 1 and waits == []                                  # not transient: no retry


# ----------------------------------------------------------------- the catalog: cache, breaker, stale
import httpx  # noqa: E402

from servers.catalog import server as catalog_mcp  # noqa: E402
from servers.catalog import lookup  # noqa: E402


def http_error(status):
    request = httpx.Request("GET", "https://example.invalid/search")
    return httpx.HTTPStatusError(f"{status}", request=request, response=httpx.Response(status, request=request))


class FakeApi:
    """Stands in for the RapidAPI call: a script of results / errors, and a count of real calls."""
    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    async def __call__(self, title):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


@contextlib.contextmanager
def catalog(api, **settings):
    keys = ("STATE_DIR", "CATALOG_BACKOFF", "CATALOG_CACHE_TTL", "CATALOG_STALE_MAX", "CATALOG_BREAKER_RESET")
    saved = {k: getattr(config, k) for k in keys}, lookup._api_search, lookup._stores
    config.STATE_DIR, config.CATALOG_BACKOFF = Path(tempfile.mkdtemp()), 0.01
    for k, v in settings.items():
        setattr(config, k, v)
    lookup._api_search, lookup._stores = api, None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        for k, v in saved[0].items():
            setattr(config, k, v)
        lookup._api_search, lookup._stores = saved[1], saved[2]


SHOWS = [{"title": "The Friend", "releaseYear": 2025, "streamingOptions": {"in": [
    {"service": {"id": "netflix"}, "link": "https://www.netflix.com/title/82182295"}]}}]


def test_transient_errors_are_retried_then_cached():
    api = FakeApi(http_error(503), httpx.ConnectTimeout("slow"), SHOWS)
    with catalog(api):
        first = asyncio.run(lookup.find_title("The Friend"))
        again = asyncio.run(lookup.find_title("the  FRIEND", "2025"))      # same key: from the cache
    assert api.calls == 3 and first == again and first[0].ids == {"netflix": FRIEND}


def test_a_rate_limit_without_a_short_retry_after_is_not_retried():
    api = FakeApi(http_error(429))
    with catalog(api):
        try:
            asyncio.run(lookup.find_title("The Friend"))
        except httpx.HTTPStatusError:
            pass
    assert api.calls == 1                     # RapidAPI quotas are monthly: an instant retry only burns quota


def test_breaker_opens_after_repeated_failures_and_fails_fast():
    api = FakeApi(httpx.ConnectError("down"))
    with catalog(api):
        for _ in range(config.CATALOG_BREAKER_FAILURES):
            try:
                asyncio.run(lookup.find_title("The Friend"))
            except httpx.ConnectError:
                pass
        calls_before = api.calls
        try:
            asyncio.run(lookup.find_title("The Friend"))
            raise AssertionError("the breaker should be open")
        except resilience.CircuitOpen as e:
            open_error = e
        try:
            asyncio.run(catalog_mcp.search_title("The Friend"))
        except Exception as e:  # noqa: BLE001   the tool's ToolError
            tool_error = str(e)
    assert calls_before == config.CATALOG_BREAKER_FAILURES * config.CATALOG_RETRIES
    assert api.calls == calls_before                                      # open: NO call at all
    assert open_error.retry_in > 0
    assert "CATALOG_UNAVAILABLE" in tool_error and "not a missing title" in tool_error


def test_breaker_half_open_lets_one_call_through():
    now = [1000.0]
    b = resilience.CircuitBreaker("x", Path(tempfile.mkdtemp()) / "b.sqlite", failures=2, reset_after=60,
                                  clock=lambda: now[0])

    async def fail():
        raise OSError("down")

    async def ok():
        return "ok"
    for _ in range(2):
        try:
            asyncio.run(b.call(fail))
        except OSError:
            pass
    assert b.state()[0] == "open"
    now[0] += 61
    assert b.state()[0] == "half_open"
    try:
        asyncio.run(b.call(fail))                    # the trial call fails: open again AT ONCE
    except OSError:
        pass
    assert b.state()[0] == "open"
    now[0] += 61
    assert asyncio.run(b.call(ok)) == "ok" and b.state()[0] == "closed"


def test_when_the_api_is_down_an_older_cached_answer_is_used():
    api = FakeApi(SHOWS, httpx.ConnectError("down"))
    with catalog(api, CATALOG_CACHE_TTL=0):              # nothing counts as fresh: always ask the API
        live = asyncio.run(lookup.find_title("The Friend"))
        stale = asyncio.run(lookup.find_title("The Friend"))    # API down: the stale copy
    assert api.calls == 1 + config.CATALOG_RETRIES and live == stale


# ----------------------------------------------------------------- a server that dies: the node is retried
def test_a_workflow_node_is_retried_when_its_server_died():
    (EVAL_STATE / "flaky_catalog_starts").unlink(missing_ok=True)
    from test_graph import run
    answer, trace = run("play The Friend 2025",
                        servers={"catalog": "evals/srv_catalog_flaky.py", "device": "evals/srv_device_dryrun.py"})
    starts = int((EVAL_STATE / "flaky_catalog_starts").read_text())
    assert starts == 2 and "simulation" in answer, (starts, answer)


# ----------------------------------------------------------------- the TV: fail fast, wait on checks
import time  # noqa: E402
import types  # noqa: E402

from servers.device import tv  # noqa: E402


@contextlib.contextmanager
def fake_tv(power_states=("Active",), window=2, post_wake=3, listening=True, webos=True):
    """A TV at 127.0.0.1: a real TCP listener on a free port (listening=False: the probe gets no answer),
    a fake webOS client whose power state goes through `power_states` (webos=False: every connect fails,
    i.e. fully OFF), and Wake-on-LAN switched off (no packets sent)."""
    loop_server = {}
    states = list(power_states)

    class Client:
        tv_state = types.SimpleNamespace(current_app_id="netflix")

        async def get_power_state(self):
            return {"state": states.pop(0) if len(states) > 1 else states[0]}

    async def fake_connect():
        loop_server["connects"] = loop_server.get("connects", 0) + 1
        if not webos:
            raise OSError("no route to host")
        return Client()

    saved = (tv.send_wake, tv.connect, tv.TV_PORTS, config.TV_WAKE_WINDOW, config.POST_WAKE_WAIT,
             os.environ.get("TV_IP"))
    tv.send_wake, tv.connect = (lambda: None), fake_connect
    config.TV_WAKE_WINDOW, config.POST_WAKE_WAIT = window, post_wake
    os.environ["TV_IP"] = "127.0.0.1"
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    if listening:
        sock.listen()
    tv.TV_PORTS = (sock.getsockname()[1],)
    try:
        yield loop_server
    finally:
        sock.close()
        (tv.send_wake, tv.connect, tv.TV_PORTS, config.TV_WAKE_WINDOW, config.POST_WAKE_WAIT, ip) = saved
        if ip is None:
            os.environ.pop("TV_IP", None)
        else:
            os.environ["TV_IP"] = ip


def timed(coro):
    start = time.monotonic()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            result = asyncio.run(coro)
        except tv.TVUnreachable as e:
            result = e
    return result, time.monotonic() - start


def test_a_tv_that_is_fully_off_fails_fast():
    with fake_tv(window=2, listening=False, webos=False) as seen:
        result, took = timed(tv.connect_with_wake())
    assert isinstance(result, tv.TVUnreachable) and "fully off" in str(result)
    assert "does not answer" in str(result)            # the probe's diagnosis is in the message
    assert took < 4, took                              # ~the 2 s window, not the old 40-90 s loop
    assert seen["connects"] >= 2                       # it kept trying within the window


def test_a_tv_that_is_on_connects_even_if_the_tcp_probe_gets_no_answer():
    # Seen live: the TV was ON, the plain TCP probe got no answer, and the old gate reported it unreachable.
    with fake_tv(listening=False, webos=True) as seen:
        result, took = timed(tv.connect_with_wake())
    assert not isinstance(result, Exception), result
    assert took < 1.5 and seen["connects"] == 1, (took, seen)


def test_a_tv_that_is_already_on_skips_the_wake_waits():
    with fake_tv(power_states=("Active",), post_wake=5):
        result, took = timed(tv.connect_with_wake())
    assert not isinstance(result, Exception) and took < 1.5, took     # was ~8 s of fixed sleeps


def test_a_waking_tv_waits_for_the_screen_then_settles():
    standby = ("Active Standby",) * 6                  # 1 at connect + 5 polls of 0.5 s = ~2.5 s of checking
    with fake_tv(power_states=standby + ("Active",), post_wake=0.5):
        result, took = timed(tv.connect_with_wake())
    assert not isinstance(result, Exception) and 2.8 <= took < 5, took    # ~0.6 wake + ~2.5 check + 0.5 settle


def _status(listening):
    from concierge.workflows import status_answer
    from servers.device import server as device_server
    with fake_tv(listening=listening, webos=False):
        with contextlib.redirect_stdout(io.StringIO()):
            s = asyncio.run(device_server.tv_status())
    return s, status_answer(s)


def test_tv_status_off_or_other_network_says_nothing_answers():
    s, answer = _status(listening=False)
    assert s["reachable"] is False and s["network_answers"] is False and "no route to host" in s["error"]
    assert "nothing answers at 127.0.0.1" in answer and "not on the TV's network" in answer, answer


def test_tv_status_on_the_network_but_refusing_says_so():
    s, answer = _status(listening=True)
    assert s["network_answers"] is True
    assert "answers at 127.0.0.1 but refused" in answer and "no route to host" in answer, answer


def test_a_fixed_wait_records_when_the_app_came_to_the_front():
    from test_tracing import MEMORY, by_name
    MEMORY.clear()
    state = {"app": "home"}
    loop = asyncio.new_event_loop()
    loop.call_later(0.6, lambda: state.update(app="netflix"))
    start = time.monotonic()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        loop.run_until_complete(tv.pause("netflix picker", 1.5, ready=lambda: state["app"] == "netflix"))
    loop.close()
    line = out.getvalue()                                                 # the log line (polled every 0.5 s)
    assert "wait netflix picker: 1.5s fixed, app in front after " in line, line
    assert 0.5 <= float(line.split("after ")[1].rstrip("s\n")) <= 1.2, line
    took = time.monotonic() - start
    span = by_name(list(MEMORY.get_finished_spans()), "tv wait netflix picker")[0]
    assert took >= 1.5                                                    # the full fixed wait still happens
    assert 0.5 <= span.attributes["tv.ready_after_s"] <= 1.2, span.attributes
    assert span.attributes["tv.wait.kind"] == "fixed"


# ----------------------------------------------------------------- tokens: refresh before expiry, retry on 401
import base64  # noqa: E402

import httpx2  # noqa: E402

from common import login  # noqa: E402


def fake_token(n: int, exp: float) -> str:
    """A JWT-shaped token (unsigned: RefreshingAuth only READS exp; servers verify signatures)."""
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part({'exp': exp, 'n': n})}.sig"


def auth_against(server, clock, lifetime=300):
    issued = []

    def fetch():
        issued.append(fake_token(len(issued) + 1, clock[0] + lifetime))
        return issued[-1]
    auth = login.RefreshingAuth(fetch, clock=lambda: clock[0])
    client = httpx2.Client(transport=httpx2.MockTransport(server), auth=auth)
    return client, auth, issued


def test_a_token_near_expiry_is_renewed_before_the_request():
    clock, seen = [1000.0], []

    def server(request):
        seen.append(request.headers["Authorization"])
        return httpx2.Response(200)
    client, auth, issued = auth_against(server, clock)
    client.get("https://mcp.invalid/")
    clock[0] += 200                                   # still valid (300 s): same token
    client.get("https://mcp.invalid/")
    clock[0] += 80                                    # 20 s left, inside the 30 s margin: renew first
    client.get("https://mcp.invalid/")
    assert len(issued) == 2 and seen == [f"Bearer {issued[0]}"] * 2 + [f"Bearer {issued[1]}"]


def test_a_401_gets_a_new_token_and_one_more_try_only():
    clock, seen = [1000.0], []
    rejected = {"remaining": 1}

    def server(request):                              # rejects the first token (e.g. Keycloak restarted)
        seen.append(request.headers["Authorization"])
        if rejected["remaining"]:
            rejected["remaining"] -= 1
            return httpx2.Response(401)
        return httpx2.Response(200)
    client, auth, issued = auth_against(server, clock)
    assert client.get("https://mcp.invalid/").status_code == 200
    assert len(issued) == 2 and seen == [f"Bearer {issued[0]}", f"Bearer {issued[1]}"]

    def always_401(request):
        seen.append(1)
        return httpx2.Response(401)
    seen.clear()
    client, auth, issued = auth_against(always_401, clock)
    assert client.get("https://mcp.invalid/").status_code == 401   # a REAL refusal is not hidden
    assert len(seen) == 2                                            # one retry, no loop


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for n, f in tests:
        f()
        print("PASS", n)
    print(f"\nAll {len(tests)} resilience tests passed.")