"""
BASELINE (Phases 1-2): ONE agent that holds every tool and every token, kept for the evals.
Day to day use concierge.graph instead (router first; 0 model calls for play/song/status; durable).

    python -m baselines.single_agent "play The Friend 2025"
    python -m baselines.single_agent --tools      # every tool from every server (no model call)
    python evals\\run_evals.py --agent single      # the eval baseline (34/42 at 2.3 calls/run, vs graph 45/45 at 0.5)

The agent is built at call time from concierge.engine, so the SERVERS / SYSTEM the evals and tests set
are picked up.
"""
import asyncio
import json
import sys

from common import config, login, tracing
from concierge import engine

SYSTEM = (
    "You are a household TV concierge for a family in India. "
    "Use search_title to find where a title is streaming, then play_title to open it. "
    "Call play_title at most once per request. "
    "If play_title returns status 'requested', say the commands were sent but that you "
    "cannot confirm playback. If the result has next_step_for_user or pressed_play is false, "
    "tell the user plainly what they still need to do (for example, press Play). "
    "If it returns 'dry_run', say it was only a simulation. "
    "If a tool returns an error, explain it plainly and do not retry the same call."
)


async def record_watch_after_play(toolbox: "engine.McpToolbox", name: str, args: dict, result_text: str):
    """after_tool hook for the concierge: bookkeeping is wired in by CODE, never left to a model."""
    if name == "play_title":
        await engine.record_watch_if_played(toolbox, args, result_text)


def concierge() -> engine.Agent:
    """The single all-in-one agent of Phases 1-2, built from the generic Agent."""
    return engine.Agent("concierge", SYSTEM, engine.SERVERS, after_tool=record_watch_after_play)


async def run_agent(user_request: str, trace: list | None = None) -> str:
    """Run one request with the concierge agent (the entry point the evals and tests use)."""
    return await concierge().run(user_request, trace)


async def show_tools():
    async with engine.McpToolbox(engine.SERVERS) as toolbox:
        print("Discovered tools:\n" + toolbox.describe())
        print("\nWhat the model will actually see (after schema simplification):")
        for t in toolbox.tools:
            print(json.dumps(t["function"], indent=2))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(__doc__)
        return
    if sys.argv[1] == "--tools":
        try:
            asyncio.run(show_tools())
        except login.LoginError as e:
            sys.exit(f"Login problem: {e}")
        return
    config.require("GROQ_API_KEY")
    if not engine.MODEL:
        sys.exit("GROQ_MODEL is not set. Run `python -m tools.groq_models`, pick a model, "
                 "and add GROQ_MODEL=<id> to .env.")
    tracing.setup("watch-concierge (single agent)")
    try:
        answer = asyncio.run(run_agent(" ".join(sys.argv[1:])))
    except BaseException as e:  # noqa: BLE001  unwrap first, then decide
        tracing.finish()
        cause = engine.first_cause(e)
        if isinstance(cause, login.LoginError):
            sys.exit(f"Login problem: {cause}")
        if isinstance(cause, engine.NotFoundError):
            sys.exit(f"Groq says model '{engine.MODEL}' is not available to your key. "
                     "Run `python -m tools.groq_models` and set GROQ_MODEL in .env.")
        if isinstance(cause, engine.APIError):
            sys.exit(f"Groq API error ({type(cause).__name__}): {cause}")
        raise
    print("\n=== final answer ===\n" + answer)
    tracing.finish()


if __name__ == "__main__":
    main()
