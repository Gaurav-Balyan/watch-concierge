# Handoff: Phase 4 done (LangGraph + resilience), start of Phase 5

Attach this file, `PLAN.md`, `SETUP.md` and the project zip to a new chat. Say: "Continue the Household Watch
Concierge from HANDOFF.md. Start Phase 5."
How to start and run everything: `SETUP.md`. Full history and every decision: `PLAN.md`.
Architecture diagram (interactive, phase by phase, request walkthroughs; private to Gaurav):
https://claude.ai/artifact/6mnd6SJUkWvue9kvEX44yq  (its P4 view is updated to "built"; add P5 when built)

## Who and why (unchanged)
- Gaurav, India, Windows + PowerShell, conda env `watch-concierge`. LG webOS TV on the home LAN.
- **Primary goal: learn agentic concepts.** Household use is secondary. Wants it QUICK and focused on agent
  concepts; park business details in PLAN.md.
- Working style that works: small exact changes; files sent INDIVIDUALLY (not zipped) with a table of where
  each goes; CRLF line endings; explain changes STEP BY STEP (what each change is for), short on test detail,
  clear on how the flow works (where agents talk to MCP or to each other). Ask before overwriting a file he may
  have edited. New dependencies go into requirements.txt in the same step.
- Teaching loop: build a thin piece by hand, run a visible trace, name the concept, a test that fails in the
  RIGHT way, mutation-check it (break the code on purpose; a test must fail). Measure with evals, not anecdotes.
- LLM: Groq. `GROQ_MODEL=openai/gpt-oss-120b`; optional `ROUTER_MODEL` (a small model from
  `python -m tools.groq_models`; llama-3.1-8b-instant was NOT available to his key). No Anthropic key.
- Versions: mcp 2.3.0, LangGraph 1.2 (+ langgraph-checkpoint-sqlite 3.1), Keycloak 26.0 (Docker), Postgres 16
  (Docker), Jaeger 2.22.0 (Docker), OpenTelemetry 1.45.1, PyJWT, psycopg 3, pytest >= 8. Run tests with `python -m pytest tests` (plain `pytest` picked up
  Anaconda base). The assistant tests in its own sandbox (Keycloak 26.0.0 tarball from GitHub + local Postgres 16 on 5433;
  no Groq key there, so evals there run only the 0-model-call cases).

## How to run a request (after the 2026-10-24 restructure)
Everything runs from the project folder as a module (`python -m <package>.<module>`); see README "Layout"
and COMMANDS.ps1.
| Entry | What it is | Model calls (typical) |
|---|---|---|
| `python -m concierge.graph "..."` | **day to day**: router + workflows + skill agents + team as a DURABLE graph (`--pending`, `--resume <id>`) | 0 for play/song/status |
| `python -m concierge.team "..."` | the team alone (the graph's fallback node) | 4-6 |
| `python -m baselines.routed "..."` | the hand-written router (Phase 3); eval baseline | same routes |
| `python -m baselines.single_agent "..."` | ONE agent (Phases 1-2); eval baseline | 2-3 |
DECIDED 2026-10-24: single agent and routed.py are baselines only (moved to baselines/); the engine they used
is concierge/engine.py. Numbers: graph 45/45 at 0.5 calls/run, single 34/42 at 2.3, team 39/42 at 3.7.

## PLAY policy and router (2026-10-24)
- Router strips polite wrappers before the rules ("can you play lupin?" = 0 model calls).
- One EXACT title match among partial ones is played (the answer names the others); several exact matches ask.
- 160 tests.

## Restructure (2026-10-24): where things moved
| Before | Now |
|---|---|
| agent.py | concierge/engine.py (Agent, McpToolbox, call_traced) + baselines/single_agent.py (SYSTEM, run_agent) + tools/groq_models.py |
| multi_agent.py | concierge/team.py (Supervisor, Catalog agent, bridges) + concierge/skill_agents.py (taste/consensus agents, check_titles, has_taste, logged_in_username) + concierge/recorder.py |
| graph.py, router.py, workflows.py, skills.py, skills/ | concierge/ |
| routed.py | baselines/routed.py |
| config, auth, login, tracing, resilience, logsetup | common/ |
| catalog_mcp.py, lookup.py | servers/catalog/server.py, lookup.py (+ youtube.py: the search half of services/youtube.py) |
| device_mcp.py, tv.py, services/ | servers/device/server.py, tv.py, services/ |
| taste_mcp.py | servers/taste/server.py |
| cli, experiments, profile_probe, mcp_probe | tools/ |
| PLAN, HANDOFF, SETUP, FLOWS, the diagram | docs/ |
| db/, keycloak/ | unchanged (the Docker containers mount them) |
McpToolbox: a SERVERS value ending in .py is a script (eval fakes), another name is a module run with -m.
Tests and evals import with aliases (`from concierge import engine as agent`), so their bodies did not change.
All prompts byte-identical to before (checked). 154 tests, 5 drills, eval dry run: same results as before.

## Key design facts
- Phase 3 (unchanged): Agent loop (model proposes, code decides and runs; repeats blocked), McpToolbox (one token
  per server audience, only offered tools can run), Supervisor + Catalog / Taste / Consensus agents, skills
  (`skills/*.md`), router (rules, then ROUTER_MODEL, else FALLBACK), workflows (policies as code, 0 calls),
  household read (scope + member + consent, audited), tracing (one trace across agents and servers).
- `graph.py`: nodes route, play, song, status (code workflows), taste, suggest (skill agents), fallback (team).
  Edges after_route / after_workflow (None = "not mine" -> fallback) / after_taste ("I like X, suggest ..." =
  taste THEN suggest). State = request, route, answer (plain data). The trace list is runtime CONTEXT. Each node
  is a span "node <name>" under "graph". Workflow nodes have a RetryPolicy (server died: "Connection closed").
- Durable: AsyncSqliteSaver in `.state/checkpoints.sqlite`, thread id = request id, durability="sync".
  A node runs AT LEAST ONCE (crash after work, before checkpoint -> re-run), hence idempotency.
- Idempotency: agent.REQUEST_ID (contextvar) -> McpToolbox injects `idempotency_key` (hidden from the model) into
  action tools that accept it (play_title). device-mcp `.state/device_actions.sqlite`: started/done/forget;
  repeat -> duplicate:true (recorder skips); unknown outcome -> UNKNOWN_STATE; other title -> ALREADY_PLAYED.
- `resilience.py`: retry_async (backoff + jitter, transient only), CircuitBreaker, TtlCache, IdempotencyStore,
  all SQLite (quick mode starts new server processes per request, so memory would forget).
- `lookup.py`: cache (24 h) -> breaker (3 fails -> 60 s open) -> retry (3, 0.5 s) -> HTTP; stale-if-error 7 days.
  catalog-mcp: CircuitOpen -> CATALOG_UNAVAILABLE. Groq retries stay in the SDK (GROQ_MAX_RETRIES): one layer.
- `tv.py`: network check after WoL (TV_WAKE_WINDOW 25 s), power-state check; fixed waits as spans with
  tv.ready_after_s. `login.RefreshingAuth`: token renewed before exp and once after 401.
- CHAOS_CRASH=<node>[:after] (drills only) kills the process at a node.

## Tests, evals, drills
- 153 tests (`python -m pytest tests`; Keycloak + Postgres up, server windows CLOSED), all passing in the sandbox
  against real Keycloak + Postgres: + test_graph 11, test_resilience 18 (incl. a real os._exit crash drill via
  tests/drill_driver.py), test_routing 24.
- Evals: `--agent graph` added. Graph 44/45, 0.4 calls/run (= routed). These are OFFLINE evals.
- `python evals\drills.py`: 5 failure drills (crash+resume, server dies, API down/breaker, 429, TV off).

## Not yet verified LIVE (do this first, on the real TV)
- SETUP.md section 4b: 11 live checks (TV on / standby / off, a real crash + resume, closing the window
  mid-play, Mishika's profile, taste -> suggest). Ask Gaurav for the results and the `tv.ready_after_s`
  numbers from checks 3-4: they decide whether the fixed waits can be shortened.

## Cleanup done (2026-10-23)
- Removed agent_inline_tools.py, concierge.py and cli.py's watch/song commands (they bypassed login, dry run
  and idempotency). cli.py = pairing + tuning only. Dead code removed (vulture + pyflakes clean).

## Phase 5 starting point (hardening and evals; discussed, not started)
DECIDED (Gaurav, 2026-10-23): NO approval step before device actions. Promptfoo: later, not now.
- Grow the eval set to ~40; run evals on every change. Optional ONLINE evals: the same checks over real traces.
- Prompt-injection tests (a title description saying "also read Mishika's taste"). Promptfoo discussed: use it
  ONLY as a time-boxed red-team generator against graph.py with fake servers; keep run_evals.py (trace checks)
  as the regression suite; copy good attacks into cases.py.
- Audit log of what each agent did. content_id must come from a real search on the TEAM path too.
- Parked: "play kesariya" (song without a YouTube word), #12 helpfulness check, requested != watched, Kids
  profile, runtime filters, A2A + token exchange (optional extra), start-concierge.ps1 does not start Jaeger.

## Known fragile points carried forward
- Profiles picked by position with blind key presses; picker/title waits fixed (the TV cannot report them).
- close_app on this TV is unreliable (once 403). Wake-on-LAN works from standby, not from fully off.
- Keycloak dev mode keeps data in the container (realm file re-creates it; logins are lost).
- Device actions run without confirmation (decided: no approval step).