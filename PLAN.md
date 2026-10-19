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

## Phase 1: IN PROGRESS (agent: Groq, plain tool-use loop, no framework)
- Step 1 DONE: hand-written agent loop (`agent.py`), dry-run tools, visible trace.
  Learned: tool results steer the model; a tool result is also an injection channel (Phase 5).
- Step 2 DONE: `catalog_mcp.py` (MCP server, stdio) + `mcp_probe.py` (model-free client).
  Learned: mcp 2.x renamed FastMCP to MCPServer; raise `ToolError` for anticipated failures
  (message reaches the model), any other exception is a crash (generic message);
  clients do not forward your environment to server subprocesses; return explicit counts, not empty lists.
- Step 3 DONE: `device_mcp.py` (`tv_status` read-only, `play_title` action).
  Dry run by default (DEVICE_DRY_RUN), input validation, honest results (verified=False),
  classified failures (TV_UNREACHABLE / TIMEOUT / UNKNOWN_STATE say whether to retry),
  one action at a time, stdout kept clean for the protocol.
- Step 4 BUILT, awaiting user's run: `agent.py` now has NO tool code. `McpToolbox` starts both
  servers, discovers tools, routes calls; refuses to start on a tool-name clash; adapts MCP JSON
  Schema for Groq (`simplify_schema`); blocks an identical failed call and a repeated action
  (uses the read_only annotation). `python agent.py --tools` shows what was discovered.
  Learned: schema adapters must not strip an ARGUMENT named "title"; annotations only help if
  servers set them (unmarked = treated as acting); server-supplied descriptions go straight into
  the model's prompt (a prompt-injection surface, Phase 5).
- Step 5 DONE, with one open question: real TV run (DEVICE_DRY_RUN=0). Netflix and Prime both play end to
  end through the agent (Prime needed PRIME_PRESS_PLAY=1 in .env and a services/prime.py that reads it);
  failure drill passed (wrong TV_IP -> TV_UNREACHABLE, no retry, model explained). tv_status reports
  "Active Standby" and "Active". Prime Play on series: plays the profile's selected/next episode, first
  profile only (2 series tried: partly watched, and new -> S1E1). A Netflix run takes ~51-53s total.
  OPEN: play_title returned TV_UNREACHABLE once from a TV believed to be in standby; it has not recurred
  in the runs since and the cause was never established. device_mcp logs to logs/device_mcp.log.
  Learned: a setting that nothing reads fails silently; don't assume a subprocess's stderr is visible;
  models paraphrase tool results loosely (say exactly what the user must do in the tool message);
  a console pipe can change Python's output encoding (UnicodeEncodeError on U+2011).
- Step 6 DONE (first baseline recorded, see 'Baseline' below): `evals/` has 11 cases (case 11 was added by the assistant to
  measure the over-hedging "you may need to press Play" seen in 4 real runs), a fake catalog + fake TV,
  a runner (repetitions, infra errors kept apart from real failures, saved results, --compare) and a
  self-test of the evals themselves. Learned: unwrap ExceptionGroups before classifying errors;
  normalise curly quotes before pattern checks; test the checks with deliberately bad models (the
  self-test found two weak checks); regexes on answers are approximate and need calibrating on real text.

## Baseline (first real eval run, model openai/gpt-oss-120b, 3 reps per case)
31/33 runs passed; no infra errors; ~3m50s for 33 runs. Saved as `evals/results/*_baseline.json`.
- Cases 1-9: 3/3 each. Case 10: 2/3 (flaky). Case 11: 2/3 (flaky).
- Case 10 fail = the model ASKED which service instead of playing. That is a policy nobody had decided
  (the code prefers Netflix; the system prompt said nothing). Not a model fault.
- Case 11 fail = "If you don't see it playing, you may need to press Play" after the tool said it pressed
  Play. 1 of 3 is too few runs to estimate a rate.
- Soft warning: "Enjoy!" in 3 runs. The model also called tv_status unprompted in 3 runs (harmless,
  read-only, but not flagged by any check).
- Cosmetic: selftest report header prints the model name from .env although it uses scripted models.

## Decisions made
- **Title on both services: the agent asks which one.** (Not "pick Netflix".) `cli.py watch` is a
  separate path and still prefers Netflix automatically; the two intentionally differ.

## Deferred: open questions (parked on purpose; the goal is learning concepts, not business detail)
Come back to these only if/when they matter. Defaults are what the assistant would do.
1. After the agent asks "Netflix or Prime?", the user's reply starts a NEW run with no memory of the title.
   a) accept it (retype the full request)   b) add a `--chat` mode that keeps conversation state  [default: b;
   it is also the natural way to learn how context carries across turns]
2. Naming the service in the request ("play Dual Title 2020 on Prime") should skip the question.
   [default: yes; add case 12 so an agent that always asks cannot pass]
3. Over-hedging ("you may need to press Play" after the tool pressed Play): forbid with one system-prompt
   line, measured by case 11. "Enjoy!" stays a soft warning only. [default as stated]
4. Order of changes: update case 10 + add case 12 and re-baseline with 10 reps; THEN add the "ask" prompt
   line and `--compare`; THEN the no-press-Play-advice line and `--compare`. One change at a time so each
   result has one explanation. [default as stated]

## Phase 1 remaining (small)
- Optional: one measured before/after change to experience the eval loop with real runs
  (`python evals/run_evals.py --only 11 --reps 10 --label before`, change ONE line, rerun, `--compare`).
- Update case 10 to the "ask" policy (decided above) when convenient.
- Then Phase 2 (identity and data): "who is asking" also fixes the first-profile-only limit on Prime/Netflix.

## Phases 2-5: NOT STARTED

## Known fragile points carried forward
- All waits are fixed sleeps, not checks (matters for Phase 4).
- `close_app` untested on this TV.
- Both flows press ENTER on the first profile; per-person profiles are a Phase 2+ concern.
- Device actions currently run without confirmation (Phase 5 adds approval).
- A retry today could relaunch a title (Phase 4 adds idempotency).