"""
Phase 4: the routed concierge as a LangGraph GRAPH, DURABLE (Steps 1-3).

    python graph.py "play The Friend 2025"                    route -> play               (0 model calls)
    python graph.py "play tum hi ho by arijit singh"          route -> song               (0 model calls)
    python graph.py "is the tv on?"                           route -> status             (0 model calls)
    python graph.py "I don't like horror"                     route -> taste              (an agent)
    python graph.py "me and Mishika want to watch something"  route -> suggest            (an agent)
    python graph.py "I like comedy, suggest a few titles"     route -> taste -> suggest   (two agents, in order)
    python graph.py "pause the tv"                            route -> fallback           (the team)
    python graph.py --draw                                    print the graph's shape (Mermaid text)
    python graph.py --pending                                 requests that did not finish (crashed)
    python graph.py --resume <request id>                     finish one of them from its last checkpoint

THE SHAPE (the same decisions as routed.py's dispatch(), now written down as data)

                         START
                           |
                         route
       +--------+--------+-+------+---------+----------+
       |        |        |        |         |          |
     play     song    status    taste --> suggest    fallback
       |        |        |        |         |          ^
       +--------+--------|--------+---------+--> END   |
       |        |                                      |
       +--------+------ "not mine" (None) -------------+

    * play / song: a code workflow; None = "not mine" -> fallback (the team), as in routed.py.
    * taste / suggest need the taste server (a login). Without it, after_route sends them to the team.
    * "I like comedy, suggest ..." (also_taste): taste FIRST (save the preference), THEN suggest.
      In routed.py that was two awaits inside one function; here it is two nodes and an edge, so
      Step 3's checkpointer saves the state BETWEEN them: a crash in suggest will not save the taste twice.

THE WORDS
    state       a small dict every node reads and returns a PART of: request, route, answer.
                LangGraph merges what a node returns into the state. Only JSON-like values go
                in it: a CHECKPOINTER (Step 3) saves it after every node (resume after a crash).
    node        one async function. Each calls OUR existing code (router, workflows, the skill
                agents, the team). No LangGraph MCP adapter: the nodes use McpToolbox as before.
    edge        "after node A, run node B". A CONDITIONAL edge is a small function of the state
                that names the next node: after_route(), after_workflow(), after_taste() below.
    context     per-run things that are NOT state (they must not be saved): the eval/test trace
                list. Passed as runtime.context, never checkpointed.

DURABLE (Step 3): a CHECKPOINTER saves the state after every node in .state/checkpoints.sqlite, under the
request's THREAD ID (printed at the start). If the process dies, `--resume <id>` loads the last saved
state and runs the node that was interrupted again. So a node runs AT LEAST ONCE: one that already did
its work but crashed before its checkpoint runs twice. That is why play_title has an idempotency key
(Step 4): the thread id is also the REQUEST ID, so the repeat is recognised and nothing launches twice.
`durability="sync"`: each checkpoint is on disk BEFORE the next node starts (the default writes it in
the background, and a crash could lose the last one). Drill it: CHAOS_CRASH=play:after (see config.py).

What LangGraph does NOT give us: OpenTelemetry spans (it reports to LangSmith). So each node
opens its own span "node <name>" under one "graph" span: the trace shows which nodes ran.
"""
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy
from mcp.shared.exceptions import MCPError
from opentelemetry import trace as otel

import agent
import config
import login
import multi_agent
import router
import tracing
import workflows


class State(TypedDict, total=False):
    request: str
    route: dict            # {"kind", "source", "reason", "params"}: plain data, so it can be saved
    answer: str | None


@dataclass
class Run:
    trace: list | None = None


def chaos(name: str, when: str):
    """Failure drill: CHAOS_CRASH=<node>[:before|after] kills the process right there, like a power cut
    (os._exit: no cleanup, no exception handlers, no last checkpoint)."""
    target, _, at = config.CHAOS_CRASH.partition(":")
    if target == name and (at or "before") == when:
        print(f"[chaos] CHAOS_CRASH={config.CHAOS_CRASH}: killing the process {when} node {name!r}", flush=True)
        os._exit(70)


def node(name):
    """Wrap a node in a span 'node <name>' (LangGraph itself emits no OpenTelemetry spans)."""
    def wrap(fn):
        async def traced(state: State, runtime: Runtime[Run]) -> dict:
            with tracing.span(f"node {name}", {"graph.node": name}) as s:
                chaos(name, "before")
                update = await fn(state, runtime.context.trace)
                chaos(name, "after")                         # the work is done, the checkpoint is not
                if update.get("answer") is None and "answer" in update:
                    s.set_attribute("graph.handover", True)      # the workflow said "not mine"
                return update
        traced.__name__ = name
        return traced
    return wrap


# ---------------------------------------------------------------- nodes (each calls existing code)

@node("route")
async def route_node(state: State, trace) -> dict:
    r = await router.route(state["request"])
    if trace is not None:   # the same "(route)" entry as routed.py: the evals' routed_as check reads it
        trace.append({"tool": "(route)", "args": {"route": r.kind, "source": r.source, "reason": r.reason},
                      "result": "", "is_error": False, "executed": False, "agent": "router"})
    print(f"[router] {r.kind} ({r.source}: {r.reason}) {r.params or ''}")
    tracing.set_attrs(otel.get_current_span(),     # the open span is 'node route'
                      {"route.kind": r.kind, "route.source": r.source, "route.reason": r.reason})
    return {"route": {"kind": r.kind, "source": r.source, "reason": r.reason, "params": dict(r.params)}}


@node("play")
async def play_node(state: State, trace) -> dict:
    return {"answer": await workflows.play_title(state["route"]["params"], trace)}


@node("song")
async def song_node(state: State, trace) -> dict:
    return {"answer": await workflows.play_song(state["route"]["params"], trace)}


@node("status")
async def status_node(state: State, trace) -> dict:
    return {"answer": await workflows.tv_status(state["route"]["params"], trace)}


@node("taste")
async def taste_node(state: State, trace) -> dict:
    return {"answer": await multi_agent.taste_agent().run(state["request"], trace)}


@node("suggest")
async def suggest_node(state: State, trace) -> dict:
    who = multi_agent.logged_in_username() or "the logged-in person"
    question = (f"Watching: {who}, plus anyone else the request names (use their lowercase usernames). "
                f"Request: {state['request']}")
    return {"answer": await multi_agent.consensus_agent(trace).run(question, trace)}


@node("fallback")
async def fallback_node(state: State, trace) -> dict:
    return {"answer": await multi_agent.run_team(state["request"], trace)}


# ---------------------------------------------------------------- edges (the decisions)

WORKFLOW_NODES = {"PLAY": "play", "SONG": "song", "STATUS": "status"}


def after_route(state: State) -> str:
    r = state["route"]
    if r["kind"] in WORKFLOW_NODES:
        return WORKFLOW_NODES[r["kind"]]
    if r["kind"] in ("TASTE", "SUGGEST") and multi_agent.has_taste():
        if r["kind"] == "SUGGEST" and not r["params"].get("also_taste"):
            return "suggest"
        return "taste"               # TASTE, or SUGGEST that also states a preference: save it first
    return "fallback"                # FALLBACK, or no taste server (no login)


def after_workflow(state: State) -> str:
    if state.get("answer") is None:
        print("[graph] the workflow did not fit: handing over to the team")
        return "fallback"
    return END


def after_taste(state: State) -> str:
    return "suggest" if state["route"]["kind"] == "SUGGEST" else END


def transient(e: Exception) -> bool:
    """A workflow node is retried only for errors that can go away: an MCP server that died or was
    restarting ("Connection closed"), a dropped connection, a timeout. Never a login problem or a bug.
    Retrying a node that may already have sent play_title is SAFE only because of the idempotency key."""
    cause = agent.first_cause(e)
    if isinstance(cause, MCPError):
        return "Connection closed" in str(cause)
    return isinstance(cause, (OSError, TimeoutError)) and not isinstance(cause, login.LoginError)


NODE_RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0, jitter=True, retry_on=transient)


def build(checkpointer=None):
    g = StateGraph(State, context_schema=Run)
    for name, fn in (("route", route_node), ("taste", taste_node), ("suggest", suggest_node),
                     ("fallback", fallback_node)):
        g.add_node(name, fn)
    # The code workflows are cheap to repeat (0 model calls) and play is idempotent: retry them when a
    # server dies. The agent nodes are NOT retried here: a repeat re-runs every model call, and Groq
    # retries are already done inside the SDK (one retry layer only).
    for name, fn in (("play", play_node), ("song", song_node), ("status", status_node)):
        g.add_node(name, fn, retry_policy=NODE_RETRY)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, ["play", "song", "status", "taste", "suggest", "fallback"])
    g.add_conditional_edges("play", after_workflow, ["fallback", END])
    g.add_conditional_edges("song", after_workflow, ["fallback", END])
    g.add_edge("status", END)                      # tv_status always answers (never "not mine")
    g.add_conditional_edges("taste", after_taste, ["suggest", END])
    g.add_edge("suggest", END)
    g.add_edge("fallback", END)
    return g.compile(checkpointer=checkpointer)


GRAPH = build()                   # no checkpointer: the evals and most tests (nothing saved)
CHECKPOINTS = "checkpoints.sqlite"


def new_request_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


async def run_graph(request: str | None, trace: list | None = None, thread_id: str | None = None,
                    checkpointer=None) -> str:
    """Same first two arguments as routed.run_routed, so the evals and tests can run either.
    With a checkpointer, the run is saved under thread_id; request=None RESUMES that thread."""
    graph = GRAPH if checkpointer is None else build(checkpointer)
    request_id = thread_id or new_request_id()
    run_config = {"configurable": {"thread_id": request_id}} if checkpointer is not None else None
    reset = agent.REQUEST_ID.set(request_id)       # the idempotency key comes from this (Step 4)
    try:
        with tracing.span("graph", {"graph.name": "concierge", "graph.request_id": request_id,
                                    "graph.resumed": request is None}):
            state = await graph.ainvoke(None if request is None else {"request": request}, run_config,
                                        context=Run(trace=trace),
                                        durability="sync" if checkpointer is not None else None)
    finally:
        agent.REQUEST_ID.reset(reset)
    return state["answer"]


def saver():
    """The durable checkpointer (an async context manager)."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(config.STATE_DIR / CHECKPOINTS))


async def run_durable(request: str | None, thread_id: str, trace: list | None = None) -> str:
    async with saver() as cp:
        if request is None:
            snap = await build(cp).aget_state({"configurable": {"thread_id": thread_id}})
            if not snap.values:
                raise SystemExit(f"No saved request {thread_id!r}. See: python graph.py --pending")
            if not snap.next:
                return snap.values.get("answer") or "(it finished with no answer)"     # nothing left to do
            print(f"[graph] resuming {thread_id}: {snap.values.get('request')!r}, next: {', '.join(snap.next)}")
        return await run_graph(request, trace, thread_id=thread_id, checkpointer=cp)


async def pending() -> list[dict]:
    """Saved requests that did not finish (their last checkpoint still has a next node)."""
    out = []
    async with saver() as cp:
        graph = build(cp)
        # Collect the ids FIRST: a lookup inside the `async for` waits for the connection the open
        # listing still holds (seen: a deadlock).
        ids = list(dict.fromkeys([t.config["configurable"]["thread_id"] async for t in cp.alist(None)]))
        for tid in ids:
            snap = await graph.aget_state({"configurable": {"thread_id": tid}})
            if snap.next:
                out.append({"id": tid, "request": snap.values.get("request"), "next": list(snap.next)})
    return sorted(out, key=lambda r: r["id"])


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return
    if sys.argv[1] == "--draw":
        print(GRAPH.get_graph().draw_mermaid())
        return
    if sys.argv[1] == "--pending":
        rows = asyncio.run(pending())
        for r in rows:
            print(f"{r['id']}  next: {', '.join(r['next']):<9} {r['request']!r}")
        print(f"{len(rows)} unfinished request(s)." + ("  Finish one: python graph.py --resume <id>" if rows else ""))
        return
    resume = sys.argv[1] == "--resume"
    if resume and len(sys.argv) != 3:
        sys.exit("Usage: python graph.py --resume <request id>   (see python graph.py --pending)")
    thread_id = sys.argv[2] if resume else new_request_id()
    request = None if resume else " ".join(sys.argv[1:])
    if not resume:
        print(f"[graph] request id {thread_id}  (if this run dies: python graph.py --resume {thread_id})")
    try:
        config.require("GROQ_API_KEY")
        tracing.setup("watch-concierge (graph)")
        answer = asyncio.run(run_durable(request, thread_id))
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