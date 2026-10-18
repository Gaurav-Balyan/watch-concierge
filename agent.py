"""
Phase 1, Step 4: the agent discovers its tools from MCP servers at run time.
LLM provider: Groq (OpenAI-style chat format).

WHAT CHANGED FROM STEP 1
    Step 1: the tool list and the tool code lived INSIDE this file.
    Step 4: this file contains NO tool code. It starts the MCP servers, asks
            each "what tools do you have?", gives the model those descriptions,
            and forwards the model's requests to whichever server owns the tool.
            Add a third server to SERVERS and its tools appear automatically.

THE LOOP IS UNCHANGED
    1. Send the model the conversation + the tools it may ask for.
    2. Model answers in text            -> done.
    3. Model asks for a tool            -> we run it (now: via MCP) and send the
                                           result back. Go to 1.

Run:
    pip install groq "mcp>=2,<3"
    python agent.py --tools                      # just show the discovered tools (no model call)
    python agent.py "play The Friend"            # device_mcp stays in DRY RUN unless DEVICE_DRY_RUN=0
    python agent.py "is the tv on?"
    python agent.py --models                     # models your Groq key can use

Needs in .env: GROQ_API_KEY, GROQ_MODEL, RAPIDAPI_KEY, TV_IP, TV_MAC.
"""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from groq import AsyncGroq, NotFoundError
from mcp import Client, StdioServerParameters

import config   # also loads .env, so GROQ_API_KEY is available

# No hard-coded default: model names come and go, so a stale default just breaks.
MODEL = os.environ.get("GROQ_MODEL", "")
MAX_TURNS = 6          # a safety limit: an agent loop must never be able to run forever
TOOL_CALL_TIMEOUT = 300   # seconds: a real TV action (wake + launch) can take over a minute

# The MCP servers this agent connects to. To add a capability, add a server here.
SERVERS = {
    "catalog": "catalog_mcp.py",
    "device": "device_mcp.py",
}

SYSTEM = (
    "You are a household TV concierge for a family in India. "
    "Use search_title to find where a title is streaming, then play_title to open it. "
    "Call play_title at most once per request. "
    "If play_title returns status 'requested', say the commands were sent but that you "
    "cannot confirm playback. If it returns 'dry_run', say it was only a simulation. "
    "If a tool returns an error, explain it plainly and do not retry the same call."
    "If the result has next_step_for_user or pressed_play is false, "
    "tell the user plainly what they still need to do (for example, press Play). "
)


# ---------------------------------------------------------------- MCP -> Groq adapter

def simplify_schema(schema):
    """Make an MCP/Pydantic JSON Schema palatable to LLM providers.

    MCP tool schemas are full JSON Schema, but providers accept different subsets.
    Pydantic emits things like  anyOf: [{type: string}, {type: null}]  for an
    optional argument, plus "title" noise. We collapse optional types to the plain
    type (optional-ness is already expressed by "required") and drop the noise.
    """
    if isinstance(schema, list):
        return [simplify_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema

    options = schema.get("anyOf")
    if options:
        non_null = [o for o in options if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(non_null) == 1:
            merged = {k: v for k, v in schema.items() if k != "anyOf"}
            merged.update(non_null[0])
            return simplify_schema(merged)

    out = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue                       # decorative label; some providers reject it
        if key == "default" and value is None:
            continue
        if key == "properties" and isinstance(value, dict):
            # The KEYS here are argument names (an argument may itself be called
            # "title"), so keep every key and only clean each argument's schema.
            out[key] = {arg: simplify_schema(sub) for arg, sub in value.items()}
            continue
        out[key] = simplify_schema(value)
    return out


def to_groq_tool(mcp_tool) -> dict:
    schema = getattr(mcp_tool, "input_schema", None) or getattr(mcp_tool, "inputSchema", None) or {}
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.name,
            "description": " ".join((mcp_tool.description or "").split()),
            "parameters": simplify_schema(schema),
        },
    }


def result_to_text(result) -> str:
    """Flatten an MCP tool result into text for the model."""
    parts = [getattr(b, "text", None) for b in result.content]
    text = "\n".join(p for p in parts if p)
    return text or "(the tool returned no content)"


# ---------------------------------------------------------------- The toolbox

class McpToolbox:
    """Starts MCP servers, discovers their tools, and routes tool calls.

    Use as:  async with McpToolbox(SERVERS) as tools: ...
    """

    def __init__(self, servers: dict[str, str]):
        self.servers = servers
        self._stack = AsyncExitStack()
        self.tools: list[dict] = []          # what the MODEL sees (Groq format)
        self.routes: dict[str, tuple[str, Client]] = {}   # tool name -> (server, client)
        self.read_only: dict[str, bool] = {}

    async def __aenter__(self):
        await self._stack.__aenter__()
        try:
            for name, script in self.servers.items():
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(config.BASE_DIR / script)],
                    cwd=str(config.BASE_DIR),
                )
                client = await self._stack.enter_async_context(Client(params))
                for t in (await client.list_tools()).tools:
                    if t.name in self.routes:
                        # Two servers claiming one name: one would silently shadow the other.
                        raise RuntimeError(
                            f"Tool name clash: '{t.name}' is offered by both "
                            f"'{self.routes[t.name][0]}' and '{name}'. Refusing to start."
                        )
                    self.routes[t.name] = (name, client)
                    ann = getattr(t, "annotations", None)
                    self.read_only[t.name] = bool(ann and getattr(ann, "read_only_hint", False))
                    self.tools.append(to_groq_tool(t))
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    def describe(self) -> str:
        lines = []
        for name, (server, _) in self.routes.items():
            kind = "read-only" if self.read_only[name] else "ACTS ON THE WORLD"
            lines.append(f"  {server}.{name}  [{kind}]")
        return "\n".join(lines)

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Forward a tool call to the server that owns it. Returns (text, is_error)."""
        route = self.routes.get(name)
        if route is None:
            return f"Unknown tool: {name}", True
        _, client = route
        try:
            result = await client.call_tool(name, args, read_timeout_seconds=TOOL_CALL_TIMEOUT)
        except Exception as e:  # noqa: BLE001   protocol-level failure (server died, timeout)
            return f"{type(e).__name__}: {e}", True
        return result_to_text(result), bool(getattr(result, "is_error", False))


# ---------------------------------------------------------------- The agent loop

async def run_agent(user_request: str) -> str:
    client = AsyncGroq()   # reads GROQ_API_KEY from the environment
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_request},
    ]
    seen_calls: dict[tuple, str] = {}   # (tool, args) -> "ok" | "error", for this request only

    async with McpToolbox(SERVERS) as toolbox:
        print("Discovered tools:\n" + toolbox.describe())

        for turn in range(1, MAX_TURNS + 1):
            response = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=toolbox.tools,
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
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    parse_error = None
                except json.JSONDecodeError as e:
                    args, parse_error = {}, f"Could not parse arguments: {e}"

                server = toolbox.routes.get(name, ("?",))[0]
                print(f"    model asks: {server}.{name}({tc.function.arguments})")

                key = (name, json.dumps(args, sort_keys=True))
                if parse_error:
                    result = parse_error
                elif seen_calls.get(key) == "error":
                    # The model repeated a call that already failed: don't run it again.
                    result = ("BLOCKED: this exact call already failed in this request. "
                              "Do not repeat it; explain the problem to the user.")
                elif seen_calls.get(key) == "ok" and not toolbox.read_only.get(name, False):
                    # An action that already succeeded: running it again could launch it twice.
                    result = ("BLOCKED: this action already ran in this request and was not repeated. "
                              "Tell the user what was done.")
                else:
                    result, is_error = await toolbox.call(name, args)
                    seen_calls[key] = "error" if is_error else "ok"

                shown = " ".join(result.split())
                shown = shown if len(shown) < 600 else shown[:600] + "..."
                print(f"    we return : {shown}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return f"Stopped after {MAX_TURNS} turns without a final answer."


# ---------------------------------------------------------------- Command line

async def show_tools():
    async with McpToolbox(SERVERS) as toolbox:
        print("Discovered tools:\n" + toolbox.describe())
        print("\nWhat the model will actually see (after schema simplification):")
        for t in toolbox.tools:
            print(json.dumps(t["function"], indent=2))


async def list_models():
    """Print the model ids this API key can use."""
    client = AsyncGroq()
    models = await client.models.list()
    for m in sorted(models.data, key=lambda m: m.id):
        print(m.id)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print(__doc__)
        return
    if sys.argv[1] == "--tools":
        asyncio.run(show_tools())
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