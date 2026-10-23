# Household Watch Concierge

Say what you want to watch, and it opens on the LG TV: a title on Netflix or Prime Video (India), or a song on
YouTube. It knows who is asking (Keycloak login), uses that person's TV profile and taste, and can suggest
something for several people. Built phase by phase as a way to learn agentic systems; the full story is in
`PLAN.md`, how to run it in `SETUP.md`, the current state in `HANDOFF.md`.

## Use it

```powershell
python graph.py "play The Friend 2025"                       # 0 model calls: router + code workflow
python graph.py "play tum hi ho by arijit singh"             # YouTube
python graph.py "is the tv on?"
python graph.py "I don't like horror"                        # saved to YOUR taste
python graph.py "me and Mishika want to watch something"     # suggestions for both (with consent)
python graph.py --pending                                    # requests a crash interrupted
python graph.py --resume <request id>                        # finish one (never launches twice)
```

## Layout

| File | Job |
|---|---|
| `graph.py` | **The entry point.** LangGraph graph: route node, a node per route, checkpoints, resume |
| `router.py` | Picks the route: rules first, an optional small model, else the team |
| `workflows.py` | PLAY, SONG, STATUS as code (0 model calls; the policies are `if` statements) |
| `skills/*.md`, `skills.py` | The Taste and Consensus agents, defined as files |
| `multi_agent.py` | The team (Supervisor + specialists through bridges): the graph's fallback |
| `agent.py` | The engine: the `Agent` loop and `McpToolbox` (tokens per server, idempotency keys) |
| `routed.py` | The same routes, hand-written (Phase 3); kept as an eval baseline |
| `catalog_mcp.py`, `lookup.py` | MCP server: where a title streams (cache, circuit breaker, retries), YouTube search |
| `device_mcp.py`, `tv.py`, `services/` | MCP server: the TV (dry run by default, idempotent play, profile per person) |
| `taste_mcp.py`, `db/` | MCP server: likes, dislikes, history in Postgres (row-level security, consent, audit) |
| `login.py`, `auth.py`, `keycloak/` | Login (PKCE), one narrow token per server, token checks, renewal |
| `resilience.py` | Retry with backoff, circuit breaker, cache, idempotency store (SQLite in `.state\`) |
| `tracing.py` | OpenTelemetry spans: one trace per request, across agents and servers (Jaeger) |
| `config.py` | Every setting, overridable from `.env` |
| `cli.py`, `experiments.py`, `profile_probe.py` | Pairing and tuning the TV only (they bypass login, dry run and idempotency) |
| `mcp_probe.py` | Call any MCP server tool without a model |
| `tests/`, `evals/` | Tests (`python -m pytest tests`), evals (`evals\run_evals.py`), failure drills (`evals\drills.py`) |

### Adding another service (JioHotstar, SonyLIV, ...)

1. Add `services/<name>.py` with `NAME`, `DEFAULT_PLAY` and `open_title(c, content_id, press_play, profile=None)`.
2. Teach `lookup.extract_ids` to read its id from the API's links, and add its id pattern to `device_mcp.ID_PATTERNS`.
3. Register it in `services/__init__.py`.

## Known limits

- The TV cannot report "picker shown" or "title page open", so those waits are fixed (each is measured in a
  trace span: tune them in `.env`). Profiles are picked by position with key presses.
- Wake-on-LAN wakes the TV from standby, not from fully off. `close_app` is unreliable on this TV.
- Prime Play is off by default (`PRIME_PRESS_PLAY`): the focused button could be rent/buy.
- A `/24` network is assumed for the wake broadcast address.

## Security

`.env`, `.webos_key`, `.login.json` and `.state\` are git-ignored. If an API key was ever pasted into a chat
or committed, regenerate it.