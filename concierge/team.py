"""
Phase 3, Steps 2-5: a Supervisor and three specialists: Catalog, Taste and Consensus.

Step 5 (Consensus, "me and Mishika, something we both like"):
    Supervisor --(ask_consensus_agent)--> Consensus agent --(MCP, taste token + household-read)--> get_household_taste
                                                 |
                                                 +--(ask_catalog_agent)--> Catalog agent --> catalog-mcp
    An agent that calls ANOTHER agent: Consensus has its own bridge to the Catalog agent.
    It proposes titles from what the model knows, then has the Catalog agent CHECK each one
    ("generate, then verify with a tool"): only confirmed titles, with real ids, are returned.

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

Today this team is the graph's FALLBACK node (concierge.graph). It can still be run on its own:
    python -m concierge.team "play The Friend 2025"
    python -m concierge.team "me and Mishika want to watch something tonight"
    python -m concierge.team --tools       # each agent's toolbox (no model call)
The Taste and Consensus agents it reaches through bridges are built in concierge.skill_agents; the
recorder is concierge.recorder.
"""
import asyncio
import sys

from common import config, login, tracing
from concierge import engine, recorder, skill_agents

CATALOG_SYSTEM = (
    "You are the Catalog agent of a household TV concierge in India. "
    "Your only job: find where a title is streaming, using search_title. "
    "You cannot play anything and must not offer to. "
    "Answer with the search result's matches as JSON, copying titles, years and ids EXACTLY "
    "as search_title returned them, for example "
    '{"matches": [{"title": "...", "year": "...", "ids": {"netflix": "..."}}]}. '
    "If nothing was found or the tool returned an error, say so plainly in one sentence. "
    "For a SONG, music video or anything asked for on YouTube, use search_youtube instead (song + artist "
    "works best) and answer with its videos as JSON, copying video_id, title and channel EXACTLY, e.g. "
    '{"youtube": [{"video_id": "...", "title": "...", "channel": "..."}]}. '
    "Never report a search result without calling the tool: if you have no tool for the request (e.g. "
    "search_youtube is missing), say exactly 'I cannot search that: the tool is not available', never "
    "'nothing was found'."
)

SUPERVISOR_SYSTEM = (
    "You are a household TV concierge for a family in India, working with a Catalog agent. "
    "You cannot search the catalog yourself: to find where a title streams, call "
    "ask_catalog_agent with a short question that includes the title, and a year ONLY if the person "
    "said one (never guess a year: a wrong year makes the search miss). "
    "Then use play_title with a content_id EXACTLY as the Catalog agent gave it. "
    "If the title is on both Netflix and Prime and the user did not say which, ask the user. "
    "Call play_title at most once per request. "
    "If play_title returns status 'requested', say the commands were sent but that you "
    "cannot confirm playback. If the result has next_step_for_user or pressed_play is false, "
    "tell the user plainly what they still need to do (for example, press Play). "
    "If it returns 'dry_run', say it was only a simulation. "
    "If a tool returns an error, explain it plainly and do not retry the same call. "
    "NEVER name a title that a specialist agent did not confirm in this request: no titles from memory, "
    "and no 'usually on' guesses. If you could not confirm any, say so."
)

CATALOG_MAX_TURNS = 4   # the Catalog agent's OWN budget, separate from the Supervisor's


def catalog_agent() -> engine.Agent:
    """A specialist: one server, one token audience (catalog-mcp)."""
    return engine.Agent("catalog", CATALOG_SYSTEM, {"catalog": engine.SERVERS["catalog"]},
                       max_turns=CATALOG_MAX_TURNS)


def ask_agent_tool(name: str, owner: str, make_agent, description: str, example: str,
                   trace: list | None, read_only: bool) -> engine.LocalTool:
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

    return engine.LocalTool(
        name=name, description=description,
        parameters={"type": "object",
                    "properties": {"question": {"type": "string", "description": example}},
                    "required": ["question"]},
        fn=ask, owner=owner, read_only=read_only)


def ask_catalog_tool(trace: list | None) -> engine.LocalTool:
    return ask_agent_tool(
        "ask_catalog_agent", "catalog agent", catalog_agent,
        "Ask the Catalog agent where a title is streaming in India (Netflix / Prime content ids), or to find "
        "a song or video on YouTube (start the question with 'Find on YouTube:'; returns video ids).",
        "e.g. 'Where is The Friend (2025) streaming?'", trace, read_only=True)


def ask_consensus_tool(trace: list | None) -> engine.LocalTool:
    return ask_agent_tool(
        "ask_consensus_agent", "consensus agent", lambda: skill_agents.consensus_agent(trace),
        "Ask the Consensus agent for titles that several people can watch together. It reads the "
        "likes and dislikes of those who agreed to share, and returns titles confirmed by the catalog, "
        "with service and content id. Include everyone's names in the question.",
        "e.g. 'Watching: gaurav, mishika. Request: something we would both like'", trace, read_only=True)


def ask_taste_tool(trace: list | None) -> engine.LocalTool:
    # read_only=False: it can change saved preferences, so the loop will not repeat an identical
    # successful call (same rule as any acting tool).
    return ask_agent_tool(
        "ask_taste_agent", "taste agent", lambda: skill_agents.taste_agent(),
        "Ask the Taste agent to save, forget or read the logged-in person's likes and dislikes. "
        "Pass the person's own words unchanged.",
        "e.g. 'I don't like horror, and I love Christopher Nolan films'", trace, read_only=False)


SUPERVISOR_CONSENSUS_LINE = (
    " For ANY request for suggestions or recommendations, for one person or several (e.g. 'suggest a "
    "comedy', 'me and Mishika want to watch something'), call ask_consensus_agent with a question that "
    "starts 'Watching: <usernames, lowercase, including the logged-in person>' followed by the request "
    "in the person's own words. If the request says 'two of us' but no names, ask the user who is "
    "watching. If the request also states a preference, save it with ask_taste_agent first. "
    "Present the titles it returns and say who was not included and why; play only when the user picks one."
)

SUPERVISOR_YOUTUBE_LINE = (
    " For a SONG or music (a song name, an artist, 'play ... on YouTube'), call ask_catalog_agent with "
    "'Find on YouTube: <the person's words>', then call play_title with service 'youtube' and the FIRST "
    "video's video_id exactly as given, unless its title clearly does not match the request (then say so "
    "and list what was found). Do not ask Netflix or Prime for a song."
)

SUPERVISOR_TASTE_LINE = (
    " For anything about the person's likes, dislikes or preferences (saving, forgetting or reading "
    "them), call ask_taste_agent and pass the person's own words unchanged. You cannot read or change "
    "preferences yourself. Report only what the Taste agent says it did."
)


def supervisor(trace: list | None = None) -> engine.Agent:
    """Only the device server, plus one bridge tool per specialist."""
    servers = {"device": engine.SERVERS["device"]}
    tools = [ask_catalog_tool(trace)]
    system = SUPERVISOR_SYSTEM + SUPERVISOR_YOUTUBE_LINE
    who = skill_agents.logged_in_username()
    if who:
        # Context for the MODEL, not authority: every server still takes the person from the token.
        # Without this line the model wrote "Watching: me, mishika" (seen live 2026-10-22).
        system += (f" The logged-in person's username is '{who}': 'me', 'I' and 'we' include {who}. "
                   "Always use usernames, never 'me' or 'user'.")
    if skill_agents.has_taste():
        tools += [ask_taste_tool(trace), ask_consensus_tool(trace)]
        system += SUPERVISOR_TASTE_LINE + SUPERVISOR_CONSENSUS_LINE
    return engine.Agent("supervisor", system, servers,
                       after_tool=recorder.record_watch_after_play, local_tools=tools)


async def run_team(request: str, trace: list | None = None) -> str:
    return await supervisor(trace).run(request, trace)


async def show_tools():
    for a in [supervisor(), catalog_agent()] + ([skill_agents.taste_agent(), skill_agents.consensus_agent()] if skill_agents.has_taste() else []):
        async with engine.McpToolbox(a.servers, a.scopes, a.offer, a.hide) as box:
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
        if not engine.MODEL:
            sys.exit("GROQ_MODEL is not set (see `python -m tools.groq_models`).")
        tracing.setup("watch-concierge (team)")
        answer = asyncio.run(run_team(" ".join(sys.argv[1:])))
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


if __name__ == "__main__":
    main()