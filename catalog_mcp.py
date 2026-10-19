"""
catalog-mcp: an MCP server that answers one question: "where can I watch this title?"

WHAT MCP IS (the short version)
    An MCP server publishes tools. Any MCP client (our agent, Claude Desktop,
    an inspector) can connect, ASK what tools exist, read their descriptions
    and input schemas, and call them. The agent no longer has the tools written
    into its own code: it discovers them at run time.

WHY A SEPARATE SERVER
    * The catalog tool can be tested without any model (see mcp_probe.py).
    * It can be given different permissions from the TV tool (Phase 2: the
      catalog agent must NOT be able to control the TV).
    * It can be swapped, moved to another machine or reused by another agent.

TRANSPORT
    Default is stdio: the client launches this file as a subprocess and
    talks to it over stdin/stdout. So this server must NEVER print() to
    stdout, because that would corrupt the protocol messages. Log to stderr.
    stdio needs no login: only a program on this PC can start the process.

    With --http (Phase 2) it listens on the network instead, and then anyone
    who can reach the port could call it. So over HTTP every request must carry
    a Keycloak token issued FOR catalog-mcp (audience + scope), checked by
    auth.McpTokenVerifier before any tool code runs. No valid token = 401.

Run it on its own (it will just wait for a client):
    python catalog_mcp.py           # stdio (what agent.py and the evals use today)
    python catalog_mcp.py --http    # HTTP on 127.0.0.1:CATALOG_MCP_PORT, tokens required
Test it with the probe client:
    python mcp_probe.py "the friend"
"""
import dataclasses
import sys

import httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer   # mcp 2.x; in mcp 1.x this was FastMCP
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import config
import lookup
from auth import McpTokenVerifier

AUDIENCE = "catalog-mcp"   # this server's name in Keycloak (a client scope in household-realm.json)

# The auth settings only take effect over HTTP; stdio ignores them.
mcp = MCPServer(
    "catalog",
    token_verifier=McpTokenVerifier(AUDIENCE),
    auth=AuthSettings(
        issuer_url=config.OIDC_ISSUER,               # told to clients in the 401, so they know where to log in
        resource_server_url=f"http://127.0.0.1:{config.CATALOG_MCP_PORT}/mcp",
        required_scopes=[AUDIENCE],                  # the token's scope must include catalog-mcp
        validate_token_resource=False,               # our verifier already checks the audience itself
    ),
)


# read_only: it changes nothing. open_world: it talks to an outside service.
# Clients use these hints (the agent skips its 'don't repeat an action' rule for read-only tools).
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def search_title(title: str, year: str | None = None) -> dict:
    """Search for a movie or series and return where it can be watched in India
    on Netflix or Prime Video (subscription only).

    Returns {"count": N, "matches": [...]}. Each match has title, year and ids
    (a dict of service -> content id). count 0 means the title was NOT FOUND on
    either service. A tool error means the lookup itself failed.

    Args:
        title: The title to search for.
        year: Optional 4-digit release year, to tell apart titles with the same name.
    """
    # The function's name, type hints and this docstring become the tool's
    # name, input schema and description. The model reads all three.
    #
    # ERRORS: raise ToolError for failures we anticipate. Its message is sent to
    # the model, which can then explain or adapt. Any OTHER exception is treated
    # as a crash: the model sees only "Error executing tool search_title" and the
    # details stay in the server log (so internals never leak to the model).
    try:
        matches = await lookup.find_title(title, year or None)   # models sometimes send "" for year
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429:
            raise ToolError(
                "Catalog lookup was rate limited (HTTP 429). "
                "Retrying later may work; retrying immediately will not."
            ) from e
        raise ToolError(f"Catalog lookup failed with HTTP {status}.") from e
    except httpx.HTTPError as e:
        raise ToolError(
            f"Catalog service unreachable ({type(e).__name__}). "
            "This is a system problem, not a missing title."
        ) from e
    # Always return an object with an explicit count: an empty list on its own
    # would reach the model as "no content at all", which is ambiguous.
    return {"count": len(matches), "matches": [dataclasses.asdict(m) for m in matches]}


if __name__ == "__main__":
    config.require("RAPIDAPI_KEY")   # fail fast with a clear message on stderr
    if "--http" in sys.argv:
        print(f"catalog-mcp starting on http://127.0.0.1:{config.CATALOG_MCP_PORT}/mcp "
              f"(tokens from {config.OIDC_ISSUER})", file=sys.stderr)
        mcp.run(transport="streamable-http", host="127.0.0.1", port=config.CATALOG_MCP_PORT)
    else:
        print("catalog-mcp starting on stdio", file=sys.stderr)
        mcp.run()   # default transport: stdio