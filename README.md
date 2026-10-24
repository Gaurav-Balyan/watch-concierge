# Household Watch Concierge

Say what you want to watch, and it opens on the LG TV: a title on Netflix or Prime Video (India), or a song on
YouTube. It knows who is asking (Keycloak login), uses that person's TV profile and taste, and can suggest
something for several people. Built phase by phase as a way to learn agentic systems; the full story is in
`docs/PLAN.md`, how to run it in `docs/SETUP.md`, the current state in `docs/HANDOFF.md`.

## Use it

Every command, with comments: `COMMANDS.ps1` (a cheat sheet to copy from, not a script).
How a request moves through the system, step by step (with and without model calls): `docs/FLOWS.md`.

```powershell
python -m concierge.graph "play The Friend 2025"                       # 0 model calls: router + code workflow
python -m concierge.graph "play tum hi ho by arijit singh"             # YouTube
python -m concierge.graph "is the tv on?"
python -m concierge.graph "I don't like horror"                        # saved to YOUR taste
python -m concierge.graph "me and Mishika want to watch something"     # suggestions for both (with consent)
python -m concierge.graph --pending                                    # requests a crash interrupted
python -m concierge.graph --resume <request id>                        # finish one (never launches twice)
```

## Layout

Run everything from the project folder, as modules: `python -m <package>.<module>` (e.g. `python -m concierge.graph`).

```
concierge/          the AGENT side (your window)
  graph.py            the entry point: LangGraph graph (route node, a node per route, checkpoints, resume)
  router.py           picks the route: rules first, an optional small model, else the team
  workflows.py        PLAY, SONG, STATUS as code (0 model calls; the policies are `if` statements)
  engine.py           the Agent loop and McpToolbox (tokens per server, idempotency keys): used by all agents
  skill_agents.py     the Taste and Consensus agents (built from skills/*.md), check_titles
  skills.py, skills/  skill files: instructions + tools + permissions + turn budget
  team.py             the Supervisor + Catalog agent + bridges: the graph's fallback
  recorder.py         after a real play, code writes the watch history
servers/            the three MCP servers (no model inside any of them)
  catalog/            server.py (search_title, search_youtube), lookup.py (cache, breaker, retries), youtube.py
  device/             server.py (tv_status, play_title: dry run, idempotent), tv.py, services/ (netflix, prime, youtube)
  taste/              server.py (likes, dislikes, history in Postgres: row-level security, consent, audit)
common/             shared by both sides: config (.env), auth (token checks), login (PKCE, renewal), tracing,
                    resilience (retry, breaker, cache, idempotency store), logsetup
tools/              cli (pairing and TV tuning: bypasses login and dry run), experiments, profile_probe,
                    mcp_probe (call a server without a model), groq_models
baselines/          single_agent.py and routed.py: earlier designs, kept only for the evals
tests/  evals/      python -m pytest tests  ·  python evals\run_evals.py  ·  python evals\drills.py
docs/               SETUP, FLOWS, PLAN (history), HANDOFF (current state), the architecture diagram
db/  keycloak/      Postgres schema and the Keycloak realm (mounted by the Docker containers: keep here)
COMMANDS.ps1        every command, with comments
```

### Adding another service (JioHotstar, SonyLIV, ...)

1. Add `servers/device/services/<name>.py` with `NAME`, `DEFAULT_PLAY` and `open_title(c, content_id, press_play, profile=None)`.
2. Teach `extract_ids` in `servers/catalog/lookup.py` to read its id from the API's links, and add its id pattern to
   `ID_PATTERNS` in `servers/device/server.py`.
3. Register it in `servers/device/services/__init__.py`.

## TV not reachable? Check the network first

If `is the tv on?` says the TV isn't answering, or a play ends with `TV_UNREACHABLE` while the TV is visibly on,
the cause is almost always the network between this PC and the TV, not the agents. Seen on 2026-10-23: the PC
had joined another Wi-Fi (`192.168.29.x`) while the TV was on `192.168.1.x`.

```powershell
# 1. Where the code looks for the TV
Select-String -Path .env -Pattern "TV_IP|TV_MAC"

# 2. Is this PC on the SAME network? The first three numbers must match TV_IP (e.g. 192.168.1.x).
#    (A 172.x address is Windows' internal virtual adapter: ignore it.)
ipconfig | Select-String "IPv4"

# 3. Can the PC reach the TV at all?
ping <TV_IP>
Test-NetConnection <TV_IP> -Port 3000          # webOS remote control (plain websocket)
Test-NetConnection <TV_IP> -Port 3001          # webOS remote control (secure websocket)

# 4. Where is the TV now? Look it up by its MAC (TV_MAC, written lower case with DASHES here).
#    Only lists devices this PC talked to recently: the TV's own screen is the sure way
#    (Settings > General > Network > Wi-Fi / Wired > Advanced settings).
arp -a | Select-String "70-97-41-9d-26-7c"

# 5. Talk to the TV directly, with no agents, MCP or login: if this fails, the problem is the network
python -m tools.cli info
```

| You see | Meaning | Fix |
|---|---|---|
| PC's IPv4 starts differently from `TV_IP` | the PC is on another Wi-Fi, a hotspot or a VPN | join the TV's Wi-Fi (and turn off "Connect automatically" for the other one), or turn the VPN off |
| Same network, `ping` fails, the TV shows a new IP | the router gave the TV a new address | update `TV_IP` in `.env`, restart the device window; reserve the address for the TV's MAC in the router (DHCP reservation) |
| `ping` works, ports 3000/3001 `False` | the TV refuses remote control | on the TV: Settings > General > Devices > External Devices > **TV On With Mobile / LG Connect Apps: on** |
| `python -m tools.cli info` works, the agents don't | the network is fine | read `logs\device_mcp.log` and the trace (`tv network check` span: `tv.last_error`, `tv.tcp_probe`) |

After changing `.env`, restart the device-mcp window: the servers read `.env` only when they start.

## Known limits

- The TV cannot report "picker shown" or "title page open", so those waits are fixed (each is measured in a
  trace span: tune them in `.env`). Profiles are picked by position with key presses.
- Wake-on-LAN wakes the TV from standby, not from fully off. `close_app` is unreliable on this TV.
- Prime Play is off by default (`PRIME_PRESS_PLAY`): the focused button could be rent/buy.
- A `/24` network is assumed for the wake broadcast address.

## Security

`.env`, `.webos_key`, `.login.json` and `.state\` are git-ignored. If an API key was ever pasted into a chat
or committed, regenerate it.
