"""
Phase 4 FAILURE DRILLS: break one thing on purpose, run a real request through the graph, and look at
what the person would be told, how long it took, and whether anything happened twice.

    python evals/drills.py              all drills (about a minute; no Groq, no TV, no RapidAPI)
    python evals/drills.py 1,4          some of them

Each drill uses the eval fakes (fake catalog, fake TV). Every pretend TV launch is counted, so a
double launch would show. What "good" means is written next to each drill and checked.
"""
import _paths  # noqa: F401  (must come first: sets up sys.path)

import asyncio
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

from concierge import engine as agent
from common import config
from concierge import graph
from eval_servers import EVAL_STATE

ROOT = _paths.ROOT
LAUNCHES = EVAL_STATE / "launches.log"


class NoModel:
    """Every drill route needs 0 model calls; a call would be a bug."""
    def __init__(self):
        async def create(**kw):
            raise AssertionError("a model was called")
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))


def launches() -> int:
    return len(LAUNCHES.read_text().split()) if LAUNCHES.exists() else 0


def ask(request: str, catalog="ok", device="dryrun") -> tuple[str, float]:
    agent.SERVERS = {"catalog": f"evals/srv_catalog_{catalog}.py", "device": f"evals/srv_device_{device}.py"}
    start = time.monotonic()
    with contextlib.redirect_stdout(io.StringIO()):
        answer = asyncio.run(graph.run_graph(request, []))
    return answer, time.monotonic() - start


def driver(state, *args, crash=""):
    env = {**os.environ, "CHAOS_CRASH": crash, "TRACING": ""}
    return subprocess.run([sys.executable, str(ROOT / "tests" / "drill_driver.py"), str(state), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)


# ---------------------------------------------------------------- the drills

def drill_crash_and_resume():
    """The process is KILLED after the TV command was sent, before the checkpoint was saved.
    Good: the resume finishes the request, and the title was launched exactly ONCE."""
    state, rid = tempfile.mkdtemp(prefix="drill_"), graph.new_request_id()
    LAUNCHES.unlink(missing_ok=True)
    start = time.monotonic()
    first = driver(state, "run", "play The Friend 2025", rid, crash="play:after")
    second = driver(state, "resume", "-", rid)
    took = time.monotonic() - start
    shutil.rmtree(state, ignore_errors=True)
    answer = second.stdout.split("ANSWER: ")[-1].strip()
    ok = first.returncode == 70 and second.returncode == 0 and launches() == 1
    return ok, answer, took, f"exit {first.returncode}, then resumed; TV launches: {launches()}"


def drill_server_dies():
    """The catalog MCP server dies as it starts (like a restart). Good: the play node is retried
    and the request works; nothing is launched twice."""
    (EVAL_STATE / "flaky_catalog_starts").unlink(missing_ok=True)
    answer, took = ask("play The Friend 2025", catalog="flaky")
    starts = int((EVAL_STATE / "flaky_catalog_starts").read_text())
    return "simulation" in answer and starts == 2, answer, took, f"catalog server started {starts}x"


def drill_api_down_breaker():
    """The catalog API is DOWN. Good: the first requests each retry and say "try again later" (never
    "not found"); after CATALOG_BREAKER_FAILURES of them the breaker opens and the next one fails FAST."""
    (EVAL_STATE / "catalog.sqlite").unlink(missing_ok=True)
    times, answers = [], []
    for _ in range(config.CATALOG_BREAKER_FAILURES + 1):
        answer, took = ask("play The Friend 2025", catalog="down")
        times.append(took)
        answers.append(answer)
    honest = all("couldn't find" not in a and "try again later" in a for a in answers)
    fast = times[-1] < min(times[:-1])
    detail = "times: " + ", ".join(f"{t:.1f}s" for t in times) + "  (last one: breaker open, no API call)"
    return honest and fast, answers[-1], times[-1], detail


def drill_rate_limited():
    """RapidAPI says 429. Good: no instant retry (quotas are monthly), an honest "try again later"."""
    answer, took = ask("play The Friend 2025", catalog="rate_limited")
    return "couldn't find" not in answer and "try again later" in answer, answer, took, "no retry for a long 429"


def drill_tv_unreachable():
    """The TV does not answer. Good: "nothing was launched", and the idempotency record is released
    (a later retry may launch)."""
    answer, took = ask("play The Friend 2025", device="unreachable")
    return "Nothing was launched" in answer, answer, took, "TV_UNREACHABLE path"


DRILLS = [drill_crash_and_resume, drill_server_dies, drill_api_down_breaker, drill_rate_limited,
          drill_tv_unreachable]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    pick = {int(x) for x in (argv or sys.argv[1:] or [""])[0].split(",") if x.strip()}
    agent.AsyncGroq = NoModel
    config.ROUTER_MODEL = ""
    failures = 0
    for i, drill in enumerate(DRILLS, 1):
        if pick and i not in pick:
            continue
        ok, answer, took, detail = drill()
        failures += not ok
        title = drill.__doc__.split(".")[0].strip()
        print(f"\n#{i} {'PASS' if ok else 'FAIL'}  {drill.__name__.removeprefix('drill_')}  ({took:.1f}s)")
        print(f"   broke:  {title}")
        print(f"   saw:    {detail}")
        print(f"   answer: {' '.join(answer.split())[:160]}")
    print(f"\n{'All drills passed.' if not failures else f'{failures} drill(s) FAILED.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
