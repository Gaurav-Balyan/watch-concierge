"""
Phase 4: the routed concierge as a LangGraph GRAPH, DURABLE (Steps 1-3).

    python -m concierge.graph "play The Friend 2025"                    route -> play               (0 model calls)
    python -m concierge.graph "play tum hi ho by arijit singh"          route -> song               (0 model calls)
    python -m concierge.graph "is the tv on?"                           route -> status             (0 model calls)
    python -m concierge.graph "I don't like horror"                     route -> taste              (an agent)
    python -m concierge.graph "me and Mishika want to watch something"  route -> suggest            (an agent)
    python -m concierge.graph "I like comedy, suggest a few titles"     route -> taste -> suggest   (two agents, in order)
    python -m concierge.graph "pause the tv"                            route -> fallback           (the team)
    python -m concierge.graph --draw                                    print the graph's shape (Mermaid text)
    python -m concierge.graph --pending                                 requests that did not finish (crashed)
    python -m concierge.graph --resume <request id>                     finish one of them from its last checkpoint
    python -m concierge.graph --reply <request id> "prime"              answer a question the concierge asked
    python -m concierge.graph --chat                                    talk in turns (replies answer questions)

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

MULTI-TURN (idea 1): when PLAY must ask ("Netflix or Prime?"), the run PAUSES instead of ending:

     play --(Ask)--> ask ==[interrupt(): the run stops, the checkpoint keeps the question + options]==
          ...the person replies (--reply / --chat), maybe minutes later, in another process...
     ==[Command(resume=reply), same thread id]==> ask --> choose --(picked)--> play the option --> END
                                                            |--(unclear)--> ask again (once)
    * SHORT-TERM MEMORY = the state of THIS thread: the question and its options (ids from the search).
      Not the chat text: the reply only SELECTS one of the saved options (code, workflows.pick_option),
      so a reply cannot introduce an id or a title. No model is involved: 0 calls per turn.
    * On resume, the paused node runs AGAIN FROM ITS FIRST LINE, and interrupt() returns the reply.
      So ask_node does nothing before interrupt(): no search, no play (they would run twice).
    * interrupt() needs a checkpointer: without --durable runs (evals, tests) an in-memory one is used.

CONVERSATIONS (idea 1c): --chat keeps ONE thread for the whole conversation; each request in it is a TURN.
    * A new request on a finished thread runs again from route, with the old state still there.
    * The REQUEST id (idempotency key) is "<thread>#<turn>" from turn 2 on: one play per TURN, and a resumed
      turn keeps its key. Conversation id != request id.
    * history: the person's words + the final answers of the last MAX_HISTORY_TURNS turns (the remember node
      writes them). Never tool results: big, and they can carry injected text. The team and the Consensus
      agent get it as earlier messages, so "play the first one" can be understood.
    * known_ids: the (service, id) pairs real searches returned in earlier turns, saved BY CODE. Each node
      starts its searched-ids set (engine.SEARCHED_IDS) with them, so the Supervisor may play an id from an
      earlier turn's search; an id that only appears in history TEXT is still blocked.

What LangGraph does NOT give us: OpenTelemetry spans (it reports to LangSmith). So each node
opens its own span "node <name>" under one "graph" span: the trace shows which nodes ran.
"""
import asyncio
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, RetryPolicy, interrupt
from mcp.shared.exceptions import MCPError
from opentelemetry import trace as otel

from concierge import engine
from common import config
from common import login
from concierge import skill_agents
from concierge import team
from concierge import router
from common import tracing
from concierge import workflows


MAX_HISTORY_TURNS = 3        # idea 1c: the last 3 turns (6 messages) are remembered; older ones are dropped
MAX_MESSAGE_CHARS = 1500     # a long answer is cut: the memory must not grow without limit
MAX_KNOWN_IDS = 50


def keep_recent(old: list | None, new: list | None) -> list:
    """The REDUCER for history: LangGraph calls it to merge a node's update into the state. Append, then trim."""
    return ((old or []) + (new or []))[-2 * MAX_HISTORY_TURNS:]


class State(TypedDict, total=False):
    request: str
    turn: int              # idea 1c: which request of this conversation (1, 2, ...): part of the request id
    history: Annotated[list, keep_recent]   # [{"role": "user"|"assistant", "content"}]: earlier turns only
    known_ids: list        # [[service, id], ...] that real searches returned in this conversation (code-written)
    route: dict            # {"kind", "source", "reason", "params"}: plain data, so it can be saved
    answer: str | None
    question: dict | None  # idea 1: {"text", "options"} while waiting for the person; None otherwise
    reply: str | None      # the person's answer to `question` (set by the ask node on resume)
    asked: int             # unclear replies so far (one re-ask, then give up)


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
            paused = None
            known = {tuple(p) for p in state.get("known_ids") or []}
            with tracing.span(f"node {name}", {"graph.node": name}) as s, \
                    engine.searched_ids_scope(known) as seen:    # idea 1c: earlier turns' searches count
                chaos(name, "before")
                try:
                    update = await fn(state, runtime.context.trace)
                except GraphInterrupt as e:
                    # Idea 1: not an error, a PAUSE. Re-raised outside the span so it is not marked failed.
                    s.set_attribute("graph.paused", True)
                    paused = e
                else:
                    chaos(name, "after")                         # the work is done, the checkpoint is not
                    if update.get("answer") is None and "answer" in update:
                        s.set_attribute("graph.handover", True)      # the workflow said "not mine"
                    if seen != known:                                # this node searched: remember the ids
                        update = {**update, "known_ids": [list(p) for p in sorted(seen)][-MAX_KNOWN_IDS:]}
            if paused is not None:
                raise paused
            return update
        traced.__name__ = name
        return traced
    return wrap


# ---------------------------------------------------------------- nodes (each calls existing code)

@node("route")
async def route_node(state: State, trace) -> dict:
    r = await router.route(state["request"])
    if state.get("history") and router.refers_back(r):
        # Idea 1c: "play the first one" is not a title: it points at an earlier turn. Only an agent with the
        # conversation can resolve it, so the team gets it (with the history).
        r = router.Route("FALLBACK", "refers to an earlier turn", "rule")
    if trace is not None:   # the same "(route)" entry as routed.py: the evals' routed_as check reads it
        trace.append({"tool": "(route)", "args": {"route": r.kind, "source": r.source, "reason": r.reason},
                      "result": "", "is_error": False, "executed": False, "agent": "router"})
    print(f"[router] {r.kind} ({r.source}: {r.reason}) {r.params or ''}")
    tracing.set_attrs(otel.get_current_span(),     # the open span is 'node route'
                      {"route.kind": r.kind, "route.source": r.source, "route.reason": r.reason})
    return {"route": {"kind": r.kind, "source": r.source, "reason": r.reason, "params": dict(r.params)}}


@node("play")
async def play_node(state: State, trace) -> dict:
    result = await workflows.play_title(state["route"]["params"], trace)
    if isinstance(result, workflows.Ask):     # idea 1: a question -> the ask node pauses the run
        return {"answer": str(result), "question": {"text": str(result), "options": result.options}, "asked": 0}
    return {"answer": result}


@node("ask")
async def ask_node(state: State, trace) -> dict:
    # NOTHING before interrupt(): on resume this node runs again from this line (see the module text).
    reply = interrupt(state["question"])         # turn 1: stops here. Resume: returns the person's reply.
    return {"reply": str(reply)}


MAX_ASKS = 2      # the first question + one re-ask after an unclear reply


@node("choose")
async def choose_node(state: State, trace) -> dict:
    question, reply = state["question"], state.get("reply") or ""
    options = question["options"]
    picked = workflows.pick_option(reply, options)
    tracing.set_attrs(otel.get_current_span(), {"choose.reply": reply,
                                                "choose.picked": picked if isinstance(picked, str) or picked is None
                                                else picked["label"]})
    if picked == "cancel":
        return {"answer": "Okay, nothing was played.", "question": None}
    if picked is None:
        asked = state.get("asked", 0) + 1
        if asked >= MAX_ASKS:
            return {"answer": "I still couldn't tell which one you meant, so nothing was played. "
                              "Ask again with the service or the year.", "question": None, "asked": asked}
        listed = "; ".join(f"{i}) {o['label']}" for i, o in enumerate(options, 1))
        text = f"Sorry, which one? {listed}. (Or say 'cancel'.)"
        return {"answer": text, "question": {**question, "text": text}, "asked": asked}
    return {"answer": await workflows.play_option(picked, trace), "question": None}


@node("song")
async def song_node(state: State, trace) -> dict:
    return {"answer": await workflows.play_song(state["route"]["params"], trace)}


@node("status")
async def status_node(state: State, trace) -> dict:
    return {"answer": await workflows.tv_status(state["route"]["params"], trace)}


@node("taste")
async def taste_node(state: State, trace) -> dict:
    return {"answer": await skill_agents.taste_agent().run(state["request"], trace)}


@node("suggest")
async def suggest_node(state: State, trace) -> dict:
    who = skill_agents.logged_in_username() or "the logged-in person"
    question = (f"Watching: {who}, plus anyone else the request names (use their lowercase usernames). "
                f"Request: {state['request']}")
    return {"answer": await skill_agents.consensus_agent(trace).run(question, trace, state.get("history"))}


@node("fallback")
async def fallback_node(state: State, trace) -> dict:
    return {"answer": await team.run_team(state["request"], trace, state.get("history"))}


@node("remember")
async def remember_node(state: State, trace) -> dict:
    """Idea 1c: the end of every turn. Write what was SAID (the request, the final answer) to the history.
    The reducer (keep_recent) appends it and drops the oldest turns."""
    said = [{"role": "user", "content": state["request"][:MAX_MESSAGE_CHARS]},
            {"role": "assistant", "content": str(state.get("answer") or "")[:MAX_MESSAGE_CHARS]}]
    return {"history": said}


# ---------------------------------------------------------------- edges (the decisions)

WORKFLOW_NODES = {"PLAY": "play", "SONG": "song", "STATUS": "status"}


def after_route(state: State) -> str:
    r = state["route"]
    if r["kind"] in WORKFLOW_NODES:
        return WORKFLOW_NODES[r["kind"]]
    if r["kind"] in ("TASTE", "SUGGEST") and skill_agents.has_taste():
        if r["kind"] == "SUGGEST" and not r["params"].get("also_taste"):
            return "suggest"
        return "taste"               # TASTE, or SUGGEST that also states a preference: save it first
    return "fallback"                # FALLBACK, or no taste server (no login)


def after_workflow(state: State) -> str:
    if state.get("answer") is None:
        print("[graph] the workflow did not fit: handing over to the team")
        return "fallback"
    return "remember"


def after_play(state: State) -> str:
    return "ask" if state.get("question") else after_workflow(state)


def after_choose(state: State) -> str:
    return "ask" if state.get("question") else "remember"      # unclear reply: ask again


def after_taste(state: State) -> str:
    return "suggest" if state["route"]["kind"] == "SUGGEST" else "remember"


def transient(e: Exception) -> bool:
    """A workflow node is retried only for errors that can go away: an MCP server that died or was
    restarting ("Connection closed"), a dropped connection, a timeout. Never a login problem or a bug.
    Retrying a node that may already have sent play_title is SAFE only because of the idempotency key."""
    cause = engine.first_cause(e)
    if isinstance(cause, MCPError):
        return "Connection closed" in str(cause)
    return isinstance(cause, (OSError, TimeoutError)) and not isinstance(cause, login.LoginError)


NODE_RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0, jitter=True, retry_on=transient)


def build(checkpointer=None):
    g = StateGraph(State, context_schema=Run)
    for name, fn in (("route", route_node), ("taste", taste_node), ("suggest", suggest_node),
                     ("fallback", fallback_node), ("remember", remember_node)):
        g.add_node(name, fn)
    # The code workflows are cheap to repeat (0 model calls) and play is idempotent: retry them when a
    # server dies. The agent nodes are NOT retried here: a repeat re-runs every model call, and Groq
    # retries are already done inside the SDK (one retry layer only).
    g.add_node("ask", ask_node)
    for name, fn in (("play", play_node), ("song", song_node), ("status", status_node), ("choose", choose_node)):
        g.add_node(name, fn, retry_policy=NODE_RETRY)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, ["play", "song", "status", "taste", "suggest", "fallback"])
    g.add_conditional_edges("play", after_play, ["ask", "fallback", "remember"])
    g.add_edge("ask", "choose")
    g.add_conditional_edges("choose", after_choose, ["ask", "remember"])
    g.add_conditional_edges("song", after_workflow, ["fallback", "remember"])
    g.add_edge("status", "remember")               # tv_status always answers (never "not mine")
    g.add_conditional_edges("taste", after_taste, ["suggest", "remember"])
    g.add_edge("suggest", "remember")
    g.add_edge("fallback", "remember")
    g.add_edge("remember", END)                    # idea 1c: every turn ends by writing the history
    return g.compile(checkpointer=checkpointer)


GRAPH = build()                   # no checkpointer: --draw and the shape test
CHECKPOINTS = "checkpoints.sqlite"


def new_request_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


async def run_graph(request: str | None, trace: list | None = None, thread_id: str | None = None,
                    checkpointer=None, reply: str | None = None) -> str:
    """Same first two arguments as routed.run_routed, so the evals and tests can run either.
    With a checkpointer, the run is saved under thread_id; request=None RESUMES that thread, and
    reply="..." answers the question it is waiting on (idea 1). The answer is the question when it pauses."""
    from langgraph.checkpoint.memory import InMemorySaver
    durable = checkpointer is not None
    graph = build(checkpointer or InMemorySaver())   # interrupt() needs SOME checkpointer
    thread = thread_id or new_request_id()
    run_config = {"configurable": {"thread_id": thread}}
    turn = (await graph.aget_state(run_config)).values.get("turn", 0)
    if reply is not None:
        what = Command(resume=reply)                # idea 1: the paused ask node gets this as interrupt()'s value
    elif request is None:
        what = None                                 # resume a crashed turn: the SAME turn, the same request id
    else:
        turn += 1                                   # idea 1c: a new request = the next turn of this thread
        what = {"request": request, "turn": turn}
    # The REQUEST id (idempotency key, Step 4). Turn 1 = the thread id, as before; later turns of a
    # conversation get their own, so a second play in the same chat is not taken for a repeat.
    request_id = thread if turn <= 1 else f"{thread}#{turn}"
    reset = engine.REQUEST_ID.set(request_id)
    try:
        with tracing.span("graph", {"graph.name": "concierge", "graph.request_id": request_id,
                                    "graph.resumed": request is None, "graph.reply": reply is not None}):
            state = await graph.ainvoke(what, run_config, context=Run(trace=trace),
                                        durability="sync" if durable else None)
    finally:
        engine.REQUEST_ID.reset(reset)
    return state["answer"]


def saver():
    """The durable checkpointer (an async context manager)."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(config.STATE_DIR / CHECKPOINTS))


async def run_durable(request: str | None, thread_id: str, trace: list | None = None,
                      reply: str | None = None) -> str:
    async with saver() as cp:
        if request is None:
            snap = await build(cp).aget_state({"configurable": {"thread_id": thread_id}})
            if not snap.values:
                raise SystemExit(f"No saved request {thread_id!r}. See: python -m concierge.graph --pending")
            if not snap.next:
                return snap.values.get("answer") or "(it finished with no answer)"     # nothing left to do
            if reply is None and snap.interrupts:
                return snap.values.get("answer")           # still waiting: show the question again
            if reply is not None and not snap.interrupts:
                raise SystemExit(f"Request {thread_id} is not waiting for an answer (use --resume).")
            print(f"[graph] {'answering' if reply is not None else 'resuming'} {thread_id}: "
                  f"{snap.values.get('request')!r}, next: {', '.join(snap.next)}")
        return await run_graph(request, trace, thread_id=thread_id, checkpointer=cp, reply=reply)


async def waiting(thread_id: str) -> bool:
    """Is this request paused on a question to the person (idea 1)?"""
    return await open_question(thread_id) is not None


async def open_question(thread_id: str) -> dict | None:
    """The question this request is paused on ({"text", "options"}), or None."""
    async with saver() as cp:
        snap = await build(cp).aget_state({"configurable": {"thread_id": thread_id}})
    return snap.values.get("question") if snap.interrupts else None


# ---------------------------------------------------------------- idea 1b: answer or new request?

_FILLER = {"the", "one", "on", "it", "that", "this", "please", "pls", "version", "movie", "film", "show",
           "series", "a", "an", "of", "in", "first", "second", "third", "last", "new", "old", "now"}


_CHANGE_OF_MIND = re.compile(r"^\s*(?:(?:actually|instead|wait|no|nah|oh|hmm|ok|okay)\b[\s,.!]*)+", re.I)


def strip_change(line: str) -> str:
    """'no, actually play lupin instead' -> 'play lupin' (change-of-mind words at the start, 'instead' at the end)."""
    return re.sub(r"[\s,]+instead\s*[.!?]*$", "", _CHANGE_OF_MIND.sub("", line), flags=re.I).strip()


def turn_kind(line: str, question: dict) -> str:
    """While a question is open, is this chat line an "answer" or a "new" request? CODE, no model.
    The routing rules are the judge: a line no rule claims ("prime", "2", "the 2021 one", "no") is an answer;
    a line a non-PLAY rule claims ("is the tv on?", "play tum hi ho by arijit") is new; a PLAY line is new only
    if its title names something the open question does not ("play lupin" vs "play the netflix one")."""
    route = router.by_rules(strip_change(line))
    if route is None:
        return "answer"
    if route.kind != "PLAY":
        return "new"
    labels = " ".join(o["label"] for o in question.get("options", [])).casefold()
    words = re.findall(r"\w+", str(route.params.get("title", "")).casefold())
    named = [w for w in words if w not in _FILLER and w not in labels]
    return "new" if named else "answer"


async def chat_turn(line: str, thread: str | None) -> tuple[str, str]:
    """One chat line -> (answer, the conversation's thread). Split from chat() so tests can drive it.
    Idea 1c: ONE thread for the whole conversation (its history and known ids live there). A line answers
    the open question, unless it is a new request (1b): then the question is closed first."""
    thread = thread or new_request_id()
    question = await open_question(thread)
    if question and turn_kind(line, question) == "new":
        # The person changed the subject: close the old question cleanly (it would wait forever).
        await run_durable(None, thread, reply="cancel")
        print(f"[chat] new request; closed the open question of {thread}")
        question, line = None, strip_change(line)
    if question:
        answer = await run_durable(None, thread, reply=line)       # the answer to the open question
    else:
        answer = await run_durable(line, thread)                   # the next turn of the conversation
    return answer, thread


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
                # Two kinds of unfinished: PAUSED on a question (normal, idea 1) or CRASHED mid-way.
                out.append({"id": tid, "request": snap.values.get("request"), "next": list(snap.next),
                            "waiting": bool(snap.interrupts)})
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
            state = "WAITING for your answer" if r["waiting"] else f"interrupted, next: {', '.join(r['next'])}"
            print(f"{r['id']}  {state:<28} {r['request']!r}")
        print(f"{len(rows)} unfinished request(s)." + ("  Answer one: --reply <id> \"...\"; finish a crashed one: "
                                                       "--resume <id>" if rows else ""))
        return
    if sys.argv[1] == "--chat":
        config.require("GROQ_API_KEY")
        tracing.setup("watch-concierge (graph chat)")
        try:
            asyncio.run(chat())
        finally:
            tracing.finish()
        return
    if sys.argv[1] == "--reply":
        if len(sys.argv) < 4:
            sys.exit('Usage: python -m concierge.graph --reply <request id> "<your answer>"')
        thread_id, reply = sys.argv[2], " ".join(sys.argv[3:])
        config.require("GROQ_API_KEY")
        tracing.setup("watch-concierge (graph)")
        try:
            answer = asyncio.run(run_durable(None, thread_id, reply=reply))
        finally:
            tracing.finish()
        print("\n=== final answer ===\n" + answer)
        show_how_to_reply(thread_id)
        return
    resume = sys.argv[1] == "--resume"
    if resume and len(sys.argv) != 3:
        sys.exit("Usage: python -m concierge.graph --resume <request id>   (see python -m concierge.graph --pending)")
    thread_id = sys.argv[2] if resume else new_request_id()
    request = None if resume else " ".join(sys.argv[1:])
    if not resume:
        print(f"[graph] request id {thread_id}  (if this run dies: python -m concierge.graph --resume {thread_id})")
    try:
        config.require("GROQ_API_KEY")
        tracing.setup("watch-concierge (graph)")
        answer = asyncio.run(run_durable(request, thread_id))
    except BaseException as e:  # noqa: BLE001
        tracing.finish()
        cause = engine.first_cause(e)
        if isinstance(cause, login.LoginError):
            sys.exit(f"Login problem: {cause}")
        if isinstance(cause, engine.APIError):
            sys.exit(f"Groq API error ({type(cause).__name__}): {cause}")
        raise
    print("\n=== final answer ===\n" + answer)
    tracing.finish()
    show_how_to_reply(thread_id)


def show_how_to_reply(thread_id: str):
    if asyncio.run(waiting(thread_id)):
        print(f'\n(waiting for your answer: python -m concierge.graph --reply {thread_id} "<your answer>")')


async def chat():
    """Turns in one window: one conversation = one thread (idea 1c). Its memory (history, known ids, an open
    question) lives in the checkpoint, so this loop only remembers the thread id."""
    print("Chat with the concierge. Empty line or 'quit' to stop.")
    open_thread = None
    while True:
        line = (await asyncio.to_thread(input, "\nyou> ")).strip()
        if not line or line.lower() in ("quit", "exit"):
            return
        answer, open_thread = await chat_turn(line, open_thread)
        print(f"concierge> {answer}")


if __name__ == "__main__":
    main()