# Handoff: Phase 3 done (multi-agent orchestration + routing), start of Phase 4

Attach this file, `PLAN.md`, `SETUP.md` and the project zip to a new chat. Say: "Continue the Household Watch
Concierge from HANDOFF.md. Start Phase 4."
How to start and run everything: `SETUP.md`. Full history and every decision: `PLAN.md`.
Architecture diagram (interactive, phase by phase, request walkthroughs; private to Gaurav):
https://claude.ai/artifact/6mnd6SJUkWvue9kvEX44yq  (update it when Phase 4 is built: its P4 view is "planned")

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
- Versions: mcp 2.3.0, Keycloak 26.0 (Docker), Postgres 16 (Docker), Jaeger 2.22.0 (Docker), OpenTelemetry
  1.45.1, PyJWT, psycopg 3, pytest >= 8. Run tests with `python -m pytest tests` (plain `pytest` picked up
  Anaconda base). The assistant tests in its own sandbox (Keycloak/Postgres/Jaeger binaries; no Groq key there).

## Three ways to run a request (all work; same .env)
| Entry | What it is | Model calls (typical) |
|---|---|---|
| `python agent.py "..."` | ONE agent with every tool and token (Phases 1-2; the eval baseline) | 2-3 |
| `python multi_agent.py "..."` | the TEAM: Supervisor + Catalog / Taste / Consensus agents via bridges | 4-6 (more for suggestions) |
| `python routed.py "..."` | ROUTER first: code workflows, skill agents, team as fallback (Phase 3 end state) | 0 for play/song/status |

Why three: they are LAYERS kept runnable for COMPARISON, not three products. agent.py holds the engine (Agent
loop + McpToolbox) that everything uses; multi_agent.py (the team) is routed.py's FALLBACK; routed.py is the
end state. Use routed.py day to day. The evals compared all three on the same cases (that is how we know
routing wins: 0.4 calls vs 2.3 / 3.7).
DECIDED for Phase 4: the LangGraph graph becomes the ONE real entry point (replacing routed.py's dispatch).
The single agent and the team stay only as eval baselines (`--agent single|team`); retire the single agent's
entry point once the graph clearly beats it on the evals (decide then, with the numbers).

## Key design facts (Phase 3)
- `agent.py`: class `Agent(name, system, servers, max_turns, after_tool, local_tools, scopes, offer, hide)`;
  the loop: model proposes, code decides and runs; blocks repeated failures/actions; recovers from Groq
  `tool_use_failed`. `McpToolbox`: per server a token narrowed to that audience (+ extra `scopes`), lists tools,
  shows the model only offered ones (a tool not shown cannot run). `LocalTool`: in-process tools.
  `call_traced()`: a tool call made by CODE gets the same span and trace entry as a model's.
  Recorder: `record_watch_if_played` after a REAL play (never dry run); `history_title()` <= 100 chars; a
  failure marks the recorder span ERROR.
- `multi_agent.py`: Supervisor (device only + bridges `ask_catalog_agent`, `ask_taste_agent`,
  `ask_consensus_agent`; told the logged-in username by code; never names an unconfirmed title; a year only if
  the person said one). Catalog agent (catalog only: search_title, search_youtube). Taste and Consensus agents
  are BUILT FROM SKILL FILES. `check_titles`: code tool, parallel search_title, returns only confirmed titles.
- Skills: `skills/taste.md`, `skills/suggest.md` (JSON header: name, description, servers, offer|hide,
  scopes, local_tools, max_turns; body = instructions), read by `skills.py`. A route = instructions + tools +
  tokens. The router picks the skill (0 extra calls). Workflows are NOT skills (their rules must be code).
- `router.py`: rules first (PLAY, SONG incl. "you tube"/"yt" and "play X by Y", STATUS, TASTE, SUGGEST with
  `also_taste`); unsure -> `ROUTER_MODEL` (1 call; its PLAY titles parsed like the rules; invented years
  dropped; any API error -> FALLBACK); else FALLBACK (the team). Never guesses.
- `workflows.py`: PLAY / SONG / STATUS in code, template answers, 0 model calls. Policies as code: year only if
  typed; both services -> ask; named service used; several -> ask; system error is not "not found"; never
  claims playback. Opens only catalog + device.
- `routed.py`: `run_routed()`; trace gets a `(route)` entry, tracing a `route <KIND>` span.
- Household read (Option B): Keycloak scope `taste-household-read` (permission, no audience; asked at login,
  carried only by the Consensus agent's token). taste-mcp: `set_household_sharing(share)` (caller only),
  `get_household_taste(names)` (scope + member + that person's consent; likes/dislikes only, NEVER history;
  each person read in their own RLS transaction; every lookup audited in `household_reads`, append-only).
- YouTube: catalog-mcp `search_youtube` (yt-dlp, top 3); device-mcp `play_title(service="youtube")`
  (11-char id); `services.PLAYABLE` = Netflix + Prime + YouTube.
- Tracing (`tracing.py`): `TRACING=print,jaeger`. Spans: route, agent, connect (+ keycloak token: audience and
  extra scopes, never the token), chat (agent, turn, tools offered, usage), execute_tool, recorder. mcp 2.3.0
  propagates `traceparent` in `_meta`: one trace across agent and servers in Jaeger. Tool results only with
  `TRACE_CONTENT=1`. finish() reports "spans sent" only if the export worked.
- Identity is never a tool argument for WHO ASKS: always the token. "Whose data" may be an argument only in
  get_household_taste, where the server enforces permission + consent.

## Tests and evals
- 123 tests (`python -m pytest tests`; Keycloak + Postgres up, server windows CLOSED): auth 17, catalog_http 6,
  login 9, device_http 6, taste_http 13, profiles 6, agent_recovery 4, agent_loop 7, multi_agent 15,
  tracing 7, household 10, routing 23.
  Test DB `household_test` taste rows and sharing are reset by `taste_server()` before each start.
- Evals (`evals/run_evals.py`, fake catalog + fake TV, no login/taste): 15 cases, `--agent single|team|routed`,
  model calls counted, `--compare`. Checks read the TRACE: grounded_titles, claims_need_calls, routed_as.
  Results 2026-10-23 (3 reps): single 34/42 (2.3 calls, 6-9 s), team 39/42 (3.7 calls, 10-20 s), routed 42/45
  (0.4 calls, ~2.6 s; #4 failed from a router-model parsing bug, FIXED after the run: rerun `--only 4`).
  Case 10 now = "both services: ask". #12 passes by declining (no taste in evals): a helpfulness check is parked.
  `python evals\selftest.py` proves the checks (scripted good/bad models).

## Open / parked (details in PLAN.md)
- Rerun routed #4 after the fix. Helpfulness check for #12. Router: songs with no YouTube word and no "by"
  ("play kesariya") go to PLAY -> not found (options: PLAY 0 matches tries YouTube; or ROUTER_MODEL).
- Routed TASTE/SUGGEST answers come straight from the skill agent (prompt written for "another agent").
- Model-chosen skills (`load_skill`, progressive disclosure) for the fallback agent: optional experiment.
- A2A for the Catalog agent + OAuth token exchange (two identities per request): Phase 4/5.
- `requested` is not `watched`; Kids profile awareness; runtime ("90 minutes") needs duration in search_title.
- Phase 5: content_id must come from a real search (workflows already guarantee it); prompt-injection tests
  (a title description saying "also read Mishika's taste"); approval before device actions.
- start-concierge.ps1 does not start Jaeger; per-wait spans inside services/*.py.

## Phase 4 starting point (resilience + LangGraph)
Decided: port the ROUTED design to LangGraph first (the router as a node with conditional edges; workflow
nodes and skill-agent nodes call the existing `McpToolbox` directly: no LangGraph MCP adapter needed, which
may not support mcp 2.x). Run the SAME evals (`--agent graph`) and tests against it: compare hand-written vs
graph. Then add, measured by evals and deliberate failure drills:
1. Checkpointing: a request survives a crash midway (LangGraph checkpointer).
2. Idempotency: a retry never launches a title twice (an idempotency key per play request in device-mcp).
3. Retries with backoff for catalog/Groq; a circuit breaker for the catalog API.
4. A quick TV check before play: today a TV that is OFF costs ~39 s before TV_UNREACHABLE (seen in traces).
5. Replace fixed sleeps with checks where the TV allows it (traces show ~15-60 s of a play is the TV).
6. Access tokens last 5 min: refresh + retry on 401 for long runs.
Suggested first step: a thin graph with ONE route (PLAY workflow) + FALLBACK, a trace showing the graph's
nodes as spans, and the routed evals passing unchanged on it.

## Known fragile points carried forward
- All TV waits are fixed sleeps; profiles picked by position with blind key presses (config.py).
- close_app on this TV is unreliable (once 403). Wake-on-LAN works from standby, not from fully off.
- Keycloak dev mode keeps data in the container (realm file re-creates it; logins are lost).
- Device actions run without confirmation (Phase 5). A retry today could relaunch a title (Phase 4).