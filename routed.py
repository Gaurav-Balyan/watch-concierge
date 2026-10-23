"""
Phase 3, Step 7: the ROUTED concierge. Router first, then the cheapest path that can do the job.

    python routed.py "play The Friend 2025"                    PLAY workflow     (0 model calls)
    python routed.py "play tum hi ho by arijit singh"          SONG workflow     (0 model calls)
    python routed.py "is the tv on?"                           STATUS workflow   (0 model calls)
    python routed.py "I don't like horror"                     TASTE skill       (an agent)
    python routed.py "me and Mishika want to watch something"  SUGGEST skill     (an agent)
    python routed.py "pause the tv"                            FALLBACK: the Phase 3 team

Compare the three designs on the same cases:
    python evals/run_evals.py --agent routed --label routed
Same .env as multi_agent.py. ROUTER_MODEL (optional) = a small model for requests the rules can't place.
"""
import asyncio
import sys

import agent
import config
import login
import multi_agent
import router
import tracing
import workflows

WORKFLOWS = {"PLAY": workflows.play_title, "SONG": workflows.play_song, "STATUS": workflows.tv_status}


async def run_routed(request: str, trace: list | None = None) -> str:
    r = await router.route(request)
    if trace is not None:   # not a tool call: checks skip entries whose name starts with "("
        trace.append({"tool": "(route)", "args": {"route": r.kind, "source": r.source, "reason": r.reason},
                      "result": "", "is_error": False, "executed": False, "agent": "router"})
    print(f"[router] {r.kind} ({r.source}: {r.reason}) {r.params or ''}")
    with tracing.span(f"route {r.kind}", {"route.kind": r.kind, "route.source": r.source,
                                          "route.reason": r.reason}):
        answer = await dispatch(r, request, trace)
    return answer


async def dispatch(r: router.Route, request: str, trace: list | None) -> str:
    if r.kind in WORKFLOWS:
        answer = await WORKFLOWS[r.kind](r.params, trace)
        if answer is not None:
            return answer
        print("[router] the workflow did not fit: handing over to the team")
    elif r.kind == "TASTE" and multi_agent.has_taste():
        return await multi_agent.taste_agent().run(request, trace)
    elif r.kind == "SUGGEST" and multi_agent.has_taste():
        if r.params.get("also_taste"):
            await multi_agent.taste_agent().run(request, trace)          # save the stated preference first
        who = multi_agent.logged_in_username() or "the logged-in person"
        question = (f"Watching: {who}, plus anyone else the request names (use their lowercase usernames). "
                    f"Request: {request}")
        return await multi_agent.consensus_agent(trace).run(question, trace)
    return await multi_agent.run_team(request, trace)                   # FALLBACK (or no taste server)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return
    try:
        config.require("GROQ_API_KEY")
        tracing.setup("watch-concierge (routed)")
        answer = asyncio.run(run_routed(" ".join(sys.argv[1:])))
    except BaseException as e:  # noqa: BLE001
        tracing.finish()
        cause = agent.first_cause(e)
        if isinstance(cause, login.LoginError):
            sys.exit(f"Login problem: {cause}")
        if isinstance(cause, agent.APIError):
            sys.exit(f"Groq API error ({type(cause).__name__}): {cause}")
        raise
    print("\n=== final answer ===\n" + answer)
    tracing.finish()


if __name__ == "__main__":
    main()