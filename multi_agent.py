"""
Phase 3, Steps 2-3: a Supervisor and two specialists, the Catalog agent and the Taste agent.

    you --> Supervisor --(ask_catalog_agent)--> Catalog agent --(MCP, catalog token)--> catalog-mcp
                 |      --(ask_taste_agent)---> Taste agent ----(MCP, taste token)----> taste-mcp
                 |
                 +--(MCP, device token)--> device-mcp
                 +--(after a real play, CODE only, no model)--(MCP, taste token)--> taste-mcp.record_watch

WHO CAN DO WHAT
    Catalog agent : ONLY catalog-mcp. It is given a catalog-mcp token and nothing else,
                    so even if it were talked into "play this", it has no play tool, and
                    its token would be refused at device-mcp (WRONG_AUDIENCE).
    Taste agent   : ONLY taste-mcp (get_my_taste, remember_taste, forget_taste). WHOSE taste is
                    decided by its token, never by the words it is sent: "remember that Mishika
                    hates horror" from Gaurav's login is saved as GAURAV's dislike.
    Supervisor    : ONLY device-mcp, plus local tools ask_catalog_agent and ask_taste_agent.
                    It has NO search_title and NO taste tools; it must ask the specialists.
    Recorder      : not an agent. After a real play, agent CODE opens a short taste-mcp
                    connection and calls record_watch. No model can trigger or skip it.

HOW THE AGENTS TALK ("agent as a tool")
    To the Supervisor's model, ask_catalog_agent looks like any other tool. When it calls it,
    our CODE runs the Catalog agent (its own loop, its own prompt, its own connections and
    token) and returns the Catalog agent's final answer as the tool result. The two models
    never see each other's conversation: only the question goes down and only the answer
    comes back. That answer is text written by a model, so the Supervisor trusts it only as
    much as a tool result (Phase 5: check that a content_id really came from search_title).

Run:
    python multi_agent.py "play The Friend 2025"
    python multi_agent.py "I don't like horror, and I love Christopher Nolan films"
    python multi_agent.py "what do you know about my taste?"
    python multi_agent.py --tools          # each agent's toolbox (no model call)
Same .env as agent.py (CATALOG_MCP_URL etc.); without URLs both use stdio, as in Phase 1.
"""
import asyncio
import json
import sys

import agent
import config
import login
import tracing

CATALOG_SYSTEM = (
    "You are the Catalog agent of a household TV concierge in India. "
    "Your only job: find where a title is streaming, using search_title. "
    "You cannot play anything and must not offer to. "
    "Answer with the search result's matches as JSON, copying titles, years and ids EXACTLY "
    "as search_title returned them, for example "
    '{"matches": [{"title": "...", "year": "...", "ids": {"netflix": "..."}}]}. '
    "If nothing was found or the tool returned an error, say so plainly in one sentence."
)

SUPERVISOR_SYSTEM = (
    "You are a household TV concierge for a family in India, working with a Catalog agent. "
    "You cannot search the catalog yourself: to find where a title streams, call "
    "ask_catalog_agent with a short question that includes the title and year if known. "
    "Then use play_title with a content_id EXACTLY as the Catalog agent gave it. "
    "If the title is on both Netflix and Prime and the user did not say which, ask the user. "
    "Call play_title at most once per request. "
    "If play_title returns status 'requested', say the commands were sent but that you "
    "cannot confirm playback. If the result has next_step_for_user or pressed_play is false, "
    "tell the user plainly what they still need to do (for example, press Play). "
    "If it returns 'dry_run', say it was only a simulation. "
    "If a tool returns an error, explain it plainly and do not retry the same call."
)

TASTE_SYSTEM = (
    "You are the Taste agent of a household TV concierge. You keep the logged-in person's viewing "
    "preferences. You do not know or need their name: the tools always act for the logged-in person. "
    "To save a preference call remember_taste ONCE PER ITEM (\"no horror, love Nolan\" = two calls: "
    "dislike 'horror', like 'Christopher Nolan'). To remove one call forget_taste. To read them call "
    "get_my_taste. You cannot search titles or play anything. "
    "Then answer in one or two sentences saying exactly what the tools did. Never say something was "
    "saved or removed unless a tool call for it succeeded. "
    "Your answer is read by another agent that CANNOT see your tool results, only your answer: "
    "include every fact it needs (for a read: the likes, the dislikes and the recently watched titles), "
    "and never write 'listed above' or 'see the result'."
)

CATALOG_MAX_TURNS = 4   # the Catalog agent's OWN budget, separate from the Supervisor's
TASTE_MAX_TURNS = 5     # a few remember_taste calls + the answer


def catalog_agent() -> agent.Agent:
    """A specialist: one server, one token audience (catalog-mcp)."""
    return agent.Agent("catalog", CATALOG_SYSTEM, {"catalog": agent.SERVERS["catalog"]},
                       max_turns=CATALOG_MAX_TURNS)


def taste_agent() -> agent.Agent:
    """A specialist: one server, one token audience (taste-mcp)."""
    return agent.Agent("taste", TASTE_SYSTEM, {"taste": agent.SERVERS["taste"]},
                       max_turns=TASTE_MAX_TURNS)


def has_taste() -> bool:
    return "taste" in agent.SERVERS          # taste-mcp is HTTP only (needs a login)


def ask_agent_tool(name: str, owner: str, make_agent, description: str, example: str,
                   trace: list | None, read_only: bool) -> agent.LocalTool:
    """A bridge from the Supervisor to a specialist: the tool's function runs a whole agent."""

    async def ask(args: dict) -> str:
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("question is empty")
        print(f"\n    >>> handing over to the {owner}: {question!r}")
        # No span code here: we are already inside the Supervisor's "execute_tool ask_..." span,
        # and the context (a contextvar) flows into this await, so the specialist's spans nest under it.
        answer = await make_agent().run(question, trace)    # a whole agent run, nested
        print(f"    <<< back to the Supervisor with: {' '.join(answer.split())[:300]}")
        return answer

    return agent.LocalTool(
        name=name, description=description,
        parameters={"type": "object",
                    "properties": {"question": {"type": "string", "description": example}},
                    "required": ["question"]},
        fn=ask, owner=owner, read_only=read_only)


def ask_catalog_tool(trace: list | None) -> agent.LocalTool:
    return ask_agent_tool(
        "ask_catalog_agent", "catalog agent", catalog_agent,
        "Ask the Catalog agent where a title is streaming in India. It returns the matching titles "
        "with their years and the Netflix / Prime content ids.",
        "e.g. 'Where is The Friend (2025) streaming?'", trace, read_only=True)


def ask_taste_tool(trace: list | None) -> agent.LocalTool:
    # read_only=False: it can change saved preferences, so the loop will not repeat an identical
    # successful call (same rule as any acting tool).
    return ask_agent_tool(
        "ask_taste_agent", "taste agent", taste_agent,
        "Ask the Taste agent to save, forget or read the logged-in person's likes and dislikes. "
        "Pass the person's own words unchanged.",
        "e.g. 'I don't like horror, and I love Christopher Nolan films'", trace, read_only=False)


async def record_watch_after_play(_supervisor_box, name: str, args: dict, result_text: str):
    """The Supervisor's after_tool hook. The Supervisor has no taste tools, so after a real play
    this CODE opens its own short taste-mcp connection (token: taste-mcp only) to record it."""
    if name != "play_title" or not has_taste():
        return
    try:
        if json.loads(result_text).get("status") != "requested":
            return                                    # dry run or failure: no connection at all
    except json.JSONDecodeError:
        return
    with tracing.span("recorder record_watch", {"recorder.reason": "real play (status requested)",
                                                "tool.args": {"title": args.get("title")}}):
        async with agent.McpToolbox({"taste": agent.SERVERS["taste"]}) as taste_box:
            await agent.record_watch_if_played(taste_box, args, result_text)


SUPERVISOR_TASTE_LINE = (
    " For anything about the person's likes, dislikes or preferences (saving, forgetting or reading "
    "them), call ask_taste_agent and pass the person's own words unchanged. You cannot read or change "
    "preferences yourself. Report only what the Taste agent says it did."
)


def supervisor(trace: list | None = None) -> agent.Agent:
    """Only the device server, plus one bridge tool per specialist."""
    servers = {"device": agent.SERVERS["device"]}
    tools = [ask_catalog_tool(trace)]
    system = SUPERVISOR_SYSTEM
    if has_taste():
        tools.append(ask_taste_tool(trace))
        system += SUPERVISOR_TASTE_LINE
    return agent.Agent("supervisor", system, servers,
                       after_tool=record_watch_after_play, local_tools=tools)


async def run_team(request: str, trace: list | None = None) -> str:
    return await supervisor(trace).run(request, trace)


async def show_tools():
    for a in [supervisor(), catalog_agent()] + ([taste_agent()] if has_taste() else []):
        async with agent.McpToolbox(a.servers) as box:
            for t in a.local_tools:
                box.add_local(t)
            print(f"[{a.name}]\n{box.describe()}\n")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return
    try:
        if sys.argv[1] == "--tools":
            asyncio.run(show_tools())
            return
        config.require("GROQ_API_KEY")
        if not agent.MODEL:
            sys.exit("GROQ_MODEL is not set (see `python agent.py --models`).")
        tracing.setup("watch-concierge (team)")
        answer = asyncio.run(run_team(" ".join(sys.argv[1:])))
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