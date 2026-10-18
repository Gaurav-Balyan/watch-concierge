"""
Reference copy of Phase 1 Step 1: the agent with its tools written INSIDE this file.
Compare with agent.py (Step 4), where the tools come from MCP servers instead.
Run: python agent_inline_tools.py "play the friend"


The whole idea is this loop:

    1. Send the model the conversation + a list of tools it may ask for.
    2. If the model answers in text            -> we are done.
    3. If the model asks to use a tool         -> OUR code runs it, and we send
                                                  the result back. Go to 1.

The model never runs anything itself. It only asks. Our code decides whether
and how to do it. Watch the trace this script prints: every turn is visible.

Run:
    pip install groq
    (put GROQ_API_KEY=... in .env, next to RAPIDAPI_KEY)
    python agent.py "play Inception"
    python agent.py "put on something from the Reacher series"
    python agent.py "play a film called zzzxqq"

Choose a model that supports tool calling and set it in .env:
    python agent.py --models          # lists the models your key can use
    GROQ_MODEL=<one of those ids>     # then add this line to .env
Not every listed model supports tool calling or chat (some are speech models),
so check Groq's docs if a model errors or ignores the tools.
"""
import asyncio
import dataclasses
import json
import os
import sys

from groq import AsyncGroq, NotFoundError

import config   # also loads .env, so GROQ_API_KEY is available
import lookup

# No hard-coded default: model names come and go, so a stale default just breaks.
MODEL = os.environ.get("GROQ_MODEL", "")
MAX_TURNS = 6   # a safety limit: an agent loop must never be able to run forever

SYSTEM = (
    "You are a household TV concierge for a family in India. "
    "Use your tools to find where a title is streaming, then play it. "
    "Never claim you played something unless play_title succeeded."
)

# ---------------------------------------------------------------- Tool definitions
# This is what the model SEES. It cannot see our code, only these descriptions,
# so the wording matters: it is how the model decides when to use each tool.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_title",
            "description": (
                "Search for a movie or series and return where it can be watched in India "
                "on Netflix or Prime Video. Returns a list of matches, each with title, year "
                "and the services it is on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The title to search for."},
                    "year": {"type": "string", "description": "Optional 4-digit release year."},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_title",
            "description": (
                "Play a title on the living-room TV. Only call this after search_title has "
                "shown the title is available on the chosen service."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "enum": ["netflix", "prime"]},
                    "title": {"type": "string", "description": "Title, for logging."},
                    "content_id": {"type": "string", "description": "The ID returned by search_title."},
                },
                "required": ["service", "title", "content_id"],
            },
        },
    },
]


# ---------------------------------------------------------------- Tool implementations
# This is what actually runs. In later steps these move into MCP servers.

async def search_title(title: str, year: str | None = None):
    matches = await lookup.find_title(title, year or None)   # models sometimes send "" for year
    return [dataclasses.asdict(m) for m in matches]


async def play_title(service: str, title: str, content_id: str):
    # DRY RUN: nothing touches the TV yet. Step 3 makes this real, safely.
    print(f"      [DRY RUN] would play '{title}' on {service} (id {content_id})")
    return {
        "status": "dry_run",
        "played": False,
        "message": (
            "This is a simulation. Nothing was attempted on the TV on purpose. "
            "Treat this as a successful test and tell the user what WOULD have played."
        ),
        "service": service,
        "title": title,
    }


TOOL_FUNCTIONS = {"search_title": search_title, "play_title": play_title}


async def run_tool(name: str, args: dict):
    """Run one tool call. Returns (result_text, is_error). Errors go BACK to the model."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return f"Unknown tool: {name}", True
    try:
        return json.dumps(await fn(**args)), False
    except Exception as e:  # noqa: BLE001
        # Don't crash: tell the model what went wrong so it can adapt or explain.
        return f"{type(e).__name__}: {e}", True


# ---------------------------------------------------------------- The agent loop

async def run_agent(user_request: str) -> str:
    client = AsyncGroq()   # reads GROQ_API_KEY from the environment
    messages = [
        {"role": "system", "content": SYSTEM},          # system prompt is just the first message
        {"role": "user", "content": user_request},
    ]

    for turn in range(1, MAX_TURNS + 1):
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=1024,
        )
        choice = response.choices[0]
        message = choice.message
        print(f"\n--- turn {turn}: finish_reason = {choice.finish_reason}")
        if message.content and message.content.strip():
            print(f"    model says: {message.content.strip()}")

        tool_calls = message.tool_calls or []
        if not tool_calls:
            return message.content or ""     # no tool requested: finished

        # Keep the model's reply (including its tool requests) in the history.
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            # The arguments arrive as a JSON STRING we must parse ourselves.
            try:
                args = json.loads(tc.function.arguments or "{}")
                parse_error = None
            except json.JSONDecodeError as e:
                args, parse_error = {}, f"Could not parse arguments: {e}"

            print(f"    model asks: {tc.function.name}({tc.function.arguments})")
            if parse_error:
                result = parse_error
            else:
                result, _is_error = await run_tool(tc.function.name, args)
            shown = result if len(result) < 300 else result[:300] + "..."
            print(f"    we return : {shown}")

            # Each tool result is its own message with role "tool".
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return f"Stopped after {MAX_TURNS} turns without a final answer."


async def list_models():
    """Print the model ids this API key can use."""
    client = AsyncGroq()
    models = await client.models.list()
    for m in sorted(models.data, key=lambda m: m.id):
        print(m.id)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    config.require("GROQ_API_KEY")
    if sys.argv[1] == "--models":
        asyncio.run(list_models())
        return
    if not MODEL:
        sys.exit("GROQ_MODEL is not set. Run `python agent.py --models`, pick a model, "
                 "and add GROQ_MODEL=<id> to .env.")
    try:
        answer = asyncio.run(run_agent(" ".join(sys.argv[1:])))
    except NotFoundError:
        sys.exit(f"Groq says model '{MODEL}' is not available to your key. "
                 "Run `python agent.py --models` and set GROQ_MODEL in .env.")
    print("\n=== final answer ===\n" + answer)


if __name__ == "__main__":
    main()