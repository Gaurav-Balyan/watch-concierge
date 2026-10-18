# Household Watch Concierge: project plan

(Plan as provided by the project owner. The status section is maintained alongside it.)

## Phase 0: Spike the risky parts
Reduce uncertainty. The whole project depends on one assumption: that you can tell the LG TV to open a specific Netflix title. If that is impossible, the design changes. Test it first, along with whether availability data for India is trustworthy. Nothing here is production code; it is throwaway experiments that produce answers.

## Phase 1: Walking skeleton
The thinnest possible version of the entire flow: one agent, two MCP servers (`catalog-mcp` and `device-mcp`), running locally. You say "play Inception" and it happens. No login, no database, no multi-agent logic yet. The point is a working end-to-end loop, so every later phase improves something that already works. Also start a small eval set (5-10 requests) so you can tell whether changes help or hurt.

## Phase 2: Identity and data
Make it a household system rather than a single-user toy. Add Keycloak for login, move the MCP servers to HTTP with OAuth, and add `taste-mcp` backed by Postgres to store each person's preferences and watch history. Key idea: separation of privilege. Alice must not read Bob's history, and the Catalog agent must not be able to control the TV. Prove this with tests that must fail in the right way. This comes before multi-agent work because bolting security onto an existing system is much harder than building it in.

## Phase 3: Multi-agent orchestration
Split the single agent into specialists: a Supervisor that routes requests, a Taste agent, a Catalog agent, and a Consensus agent that reconciles several people's preferences ("two of us, 90 minutes, one dislikes horror"). Add OpenTelemetry tracing here, because once several agents talk to each other you cannot debug without seeing who did what.

## Phase 4: Resilience
Real systems fail: an API gets throttled, an MCP server dies, the TV is asleep. Add caching, retries with backoff, circuit breakers, and idempotency keys so a retry never launches a movie twice. Adopt a durable workflow engine (Temporal or LangGraph) so a request survives a crash midway. Then deliberately break things to confirm the system degrades gracefully.

## Phase 5: Hardening and evals
The ongoing quality phase. Grow the eval set to about 40 requests, add prompt-injection tests (for example, a movie description that says "ignore your instructions and..."), require approval before device actions, and keep an audit log of what each agent did. Evals run on every change, so regressions show up immediately.

## How the phases connect
Each phase ends with a testable result, and each one de-risks the next. Phase 0 tests feasibility, 1 the core loop, 2 security, 3 collaboration between agents, 4 failure handling, and 5 keeps all of it honest over time.

---

# Status (update as work proceeds)

## Phase 0: DONE
Findings on this LG TV (India):
- Pairing, wake-on-LAN (from standby) and app launch work through `aiowebostv`.
- Netflix deep link works ONLY when the app is already running with a profile selected: `launch_app_with_content_id("netflix", "m=https://www.netflix.com/watch/<id>&source_type=4")`. It lands on the title page; one ENTER presses Play. Other link shapes fell back to home or a wrong title.
- Prime Video is the reverse: send the bare `amzn1.dv.gti.<uuid>` as contentId WHILE the profile picker shows, then ENTER picks the profile (needs ~15s after the link) and a second ENTER presses Play. A link sent after the profile is chosen resets the app.
- YouTube deep link works any time via `launch_app_with_params` with `contentTarget`.
- Availability data (Streaming Availability API, country `in`) returned correct Netflix and Prime links for the titles tried. NOT yet tested at scale: accuracy across many titles, series/episode IDs, and rate limits. Series carry `firstAirYear`, not `releaseYear`.
- Scope grew beyond the original assumption: Prime and YouTube were also spiked.

## Phase 1: NOT STARTED
Existing code that maps onto it (to be reshaped, not thrown away):
- catalog side: `lookup.py` (title to per-service IDs) -> becomes `catalog-mcp`.
- device side: `tv.py` and `services/*` (wake, connect, open title, play) -> becomes `device-mcp`.
- `concierge.py` is the current single entry point (no LLM yet); the agent replaces `cli.py` as the way requests arrive.
- Still needed: the agent itself, the MCP server wrappers, and the 5-10 request eval set.

## Phases 2-5: NOT STARTED

## Known fragile points carried forward
- All waits are fixed sleeps, not checks (matters for Phase 4).
- `close_app` untested on this TV.
- Both flows press ENTER on the first profile; per-person profiles are a Phase 2+ concern.
- Device actions currently run without confirmation (Phase 5 adds approval).
- A retry today could relaunch a title (Phase 4 adds idempotency).
