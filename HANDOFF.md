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
  `python agent.py --models`; llama-3.1-8b-instant was NOT available to his key). No Anthropic key.
- Versions: mcp 2.3.0, LangGraph 1.2 (+ langgraph-checkpoint-sqlite 3.1), Keycloak 26.0 (Docker), Postgres 16
  (Docker), Jaeger 2.22.0 (Docker), OpenTelemetry 1.45.1, PyJWT, psycopg 3, pytest >= 8. Run tests with `python -m pytest tests` (plain `pytest` picked up
  Anaconda base). The assistant tests in its own sandbox (Keycloak 26.0.0 tarball from GitHub + local Postgres 16 on 5433;
  no Groq key there, so evals there run only the 0-model-call cases).

## How to run a request (Phase 4 end state)
| Entry | What it is | Model calls (typical) |
|---|---|---|
| `python graph.py "..."` | **day to day**: the routed design as a DURABLE LangGraph graph (`--pending`, `--resume <id>`) | 0 for play/song/status |
| `python routed.py "..."` | the same routes, hand-written dispatch (Phase 3 end state); kept for comparison | same |
| `python multi_agent.py "..."` | the TEAM: the graph's fallback node | 4-6 |
| `python agent.py "..."` | ONE agent (Phases 1-2): agent.py is also the ENGINE (Agent loop + McpToolbox) | 2-3 |
OPEN DECISION (Gaurav): retire `agent.py "<request>"` and `routed.py` as entry points (code stays; eval baselines).
Numbers: graph 44/45 at 0.4 calls/run, single 34/42 at 2.3, team 39/42 at 3.7.

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