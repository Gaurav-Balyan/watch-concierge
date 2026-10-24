"""
mcp_probe.py: a tiny MCP client for testing a server WITHOUT any model.

It does what an agent's MCP layer does:
    1. launch the server as a subprocess (stdio)
    2. ask it what tools it has          (discovery)
    3. optionally call one               (execution)

Usage:
    python -m tools.mcp_probe                                   # list catalog tools
    python -m tools.mcp_probe "the friend" [year]               # catalog: search_title (short form)
    python -m tools.mcp_probe catalog search_title title="the friend" year=2025

    python -m tools.mcp_probe device                            # list device tools
    python -m tools.mcp_probe device tv_status                  # read-only TV check
    python -m tools.mcp_probe device play_title service=netflix content_id=82182295
    python -m tools.mcp_probe device play_title service=prime content_id=notvalid   # validation test

Arguments after the tool name are key=value pairs.
Note: device_mcp stays in DRY RUN unless DEVICE_DRY_RUN=0 is set in .env.
"""
import asyncio
import json
import sys

from mcp import Client, StdioServerParameters

from common import config

SERVERS = {"catalog": "servers.catalog.server", "device": "servers.device.server"}


def server_params(name: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,                      # the same Python that runs this script
        args=["-m", SERVERS[name]],                  # a module, run from the project root
        cwd=str(config.BASE_DIR),
    )


def parse_args(pairs: list[str]) -> dict:
    args = {}
    for p in pairs:
        if "=" not in p:
            sys.exit(f"Argument {p!r} must look like key=value")
        k, v = p.split("=", 1)
        args[k] = v
    return args


def parse_command_line(argv: list[str]):
    """Return (server, tool, arguments). tool is None when we should only list tools."""
    if len(argv) > 1 and argv[1] in SERVERS:
        server, rest = argv[1], argv[2:]
        return server, (rest[0] if rest else None), parse_args(rest[1:])
    # Legacy short form: python -m tools.mcp_probe "the friend" [year]
    if len(argv) > 1:
        args = {"title": argv[1]}
        if len(argv) > 2:
            args["year"] = argv[2]
        return "catalog", "search_title", args
    return "catalog", None, {}


async def main():
    server, tool, args = parse_command_line(sys.argv)
    async with Client(server_params(server)) as client:
        tools = (await client.list_tools()).tools
        print(f"[{server}] server exposes {len(tools)} tool(s):\n")
        for t in tools:
            print(f"* {t.name}")
            print(f"  description: {' '.join((t.description or '').split())}")
            schema = t.input_schema if hasattr(t, "input_schema") else t.inputSchema
            print(f"  input schema: {json.dumps(schema)}")
            ann = getattr(t, "annotations", None)
            if ann is not None:
                print(f"  annotations: {ann.model_dump(exclude_none=True)}")
            print()

        if tool:
            print(f"Calling {tool}({args}) ...")
            result = await client.call_tool(tool, args)
            print("is_error:", getattr(result, "is_error", getattr(result, "isError", None)))
            for block in result.content:
                print(getattr(block, "text", block))


if __name__ == "__main__":
    asyncio.run(main())