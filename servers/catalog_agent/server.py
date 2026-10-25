"""
catalog-agent: the Catalog AGENT as its own process, reached over A2A (idea 2, step 2a).

WHAT CHANGES (and what does not)
    Before: the Supervisor's ask_catalog_agent tool ran the Catalog agent as a Python call, in the same process.
    Now:    the same agent (concierge.team.catalog_agent: same prompt, same model loop, its own MCP connection to
            catalog-mcp) runs HERE, behind an A2A server. The Supervisor's tool sends it a MESSAGE over HTTP.
    MCP vs A2A: MCP connects an agent to TOOLS (catalog-mcp has no model). A2A connects an agent to another AGENT
    (this one thinks: it has a model and decides which search to run).

THE A2A WORDS (spec 1.0, a2a-sdk 1.2)
    Agent Card  a public JSON description at /.well-known/agent-card.json: name, skills, where to send messages.
                A client reads it first (DISCOVERY), as an MCP client lists tools.
    Message     what the caller sends: here the question in text ("Where is The Friend (2025) streaming?").
    Task        what the server makes of it: an id + a STATE (WORKING -> COMPLETED | FAILED), status messages,
                and ARTIFACTS (the results). The caller gets the final task back.
    Artifacts   here two: "answer" (TEXT, written by the model) and "searched_ids" (DATA, written by CODE: the
                (service, id) pairs this agent's searches really returned). The caller trusts only the second for
                provenance (Phase 5, step 1): model text stays text, even after a network hop.

NOT YET (step 2b): no login. The server listens on 127.0.0.1 only, so only programs on this PC can reach it, and
any of them can. 2b adds a token for audience catalog-agent and "the Supervisor on behalf of <person>".

Run:   python -m servers.catalog_agent.server          (127.0.0.1:CATALOG_AGENT_PORT, default 8004)
       then set CATALOG_AGENT_URL=http://127.0.0.1:8004 for the concierge.
"""
import sys

from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, TaskState
from starlette.applications import Starlette

from common import config
from common import tracing
from concierge import engine
from concierge import team


def agent_card(url: str) -> AgentCard:
    """What this agent tells the world about itself. The SKILLS are descriptions for a caller (a person or a
    model choosing whom to ask), like an MCP tool's description; they are not code."""
    return AgentCard(
        name="catalog-agent",
        description="Finds where a movie or series streams in India (Netflix, Prime Video), or a song/video on "
                    "YouTube. It cannot play anything.",
        version="1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[
            AgentSkill(id="find_title", name="Find a title",
                       description="Where a movie or series is streaming in India, with Netflix / Prime content ids.",
                       tags=["catalog", "netflix", "prime"], examples=["Where is The Friend (2025) streaming?"]),
            AgentSkill(id="find_youtube", name="Find a song or video",
                       description="Find a song or video on YouTube; returns video ids. Start with 'Find on YouTube:'.",
                       tags=["youtube", "music"], examples=["Find on YouTube: tum hi ho arijit singh"]),
        ],
    )


class CatalogAgentExecutor(AgentExecutor):
    """The bridge between A2A (messages, tasks) and our Agent class (a question in, an answer out)."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)                    # the task exists: state SUBMITTED
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(state=TaskState.TASK_STATE_WORKING,
                                    message=new_text_message("Searching the catalog."))
        question = get_message_text(context.message).strip()
        try:
            if not question:
                raise ValueError("the message has no text")
            # The searched ids are written by CODE (engine.McpToolbox.call), as in the Supervisor's process.
            # A FRESH set, always: this agent must never see (or fill) a caller's set, even when a test runs
            # the server in the caller's process. Across the network, ids travel ONLY in the artifact below.
            searched = set()
            token = engine.SEARCHED_IDS.set(searched)
            try:
                answer = await team.catalog_agent().run(question)
            finally:
                engine.SEARCHED_IDS.reset(token)
        except Exception as e:  # noqa: BLE001   the caller must hear WHY, as a FAILED task, never a hang
            cause = engine.first_cause(e)
            await updater.update_status(state=TaskState.TASK_STATE_FAILED,
                                        message=new_text_message(f"catalog-agent failed: {type(cause).__name__}: {cause}"))
            return
        await updater.add_artifact(parts=[new_text_part(text=answer, media_type="text/plain")], name="answer")
        await updater.add_artifact(name="searched_ids", parts=[new_data_part(
            {"searched_ids": [list(p) for p in sorted(searched)]}, media_type="application/json")])
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported")      # a search takes seconds: not worth it yet


def build_app(url: str) -> Starlette:
    """The A2A web app: the card (GET /.well-known/agent-card.json) + JSON-RPC (POST /). Tests run it in-process."""
    card = agent_card(url)
    handler = DefaultRequestHandler(agent_executor=CatalogAgentExecutor(), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])


def main():
    import uvicorn
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    config.require("GROQ_API_KEY")                  # this agent has its own model
    if not engine.MODEL:
        sys.exit("GROQ_MODEL is not set (see `python -m tools.groq_models`).")
    tracing.setup("catalog-agent")
    url = f"http://127.0.0.1:{config.CATALOG_AGENT_PORT}"
    print(f"catalog-agent (A2A) on {url}  card: {url}/.well-known/agent-card.json  "
          f"(no login yet: this PC only)", flush=True)
    try:
        uvicorn.run(build_app(url), host="127.0.0.1", port=config.CATALOG_AGENT_PORT, log_level="warning")
    finally:
        tracing.finish()


if __name__ == "__main__":
    main()
