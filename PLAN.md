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

New chat? Read `HANDOFF.md` first: it has the current state and the next starting point.

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

## Phase 2: DONE (identity and data), completed 2026-10-20
Users in realm `household`: gaurav (sub 1111...), mishika (sub 2222...). Dev passwords `<name>-dev-pass`.
- Step 1 DONE: Keycloak 26.0 in Docker, dev mode (HTTP, built-in DB; production needs HTTPS + Postgres).
  `docker run ... -v "${PWD}\keycloak:/opt/keycloak/data/import" quay.io/keycloak/keycloak:26.0 start-dev --import-realm`
  Learned: the issuer publishes discovery + JWKS; servers verify tokens locally with the public key (kid picks the key).
- Step 2 DONE: `keycloak/household-realm.json` (realm, public client `concierge-cli` with PKCE S256, two users).
  Learned: a JWT is signed JSON: iss, sub, azp, exp/iat; the first token had NO `aud`.
- Step 3 DONE: one client scope per MCP server (catalog-mcp, device-mcp, taste-mcp) adds that `aud`; the agent
  requests only the scope it needs. Fixed user ids so `sub` survives re-import.
  Learned: listing clientScopes in an import drops ALL Keycloak defaults (even `sub`), so `basic` and `profile`
  are defined explicitly; tokens now carry only sub + preferred_username.
- Step 4 DONE: `auth.py` TokenVerifier (RS256 only, iss, aud, exp, sub; fixed AuthError codes) +
  `tests/test_auth.py` (17 incl. 4 live). Tests were mutation-checked (weakened verifier must fail them).
  Learned: test the REASON for a rejection, not just rejection; alg=none and HS256-with-public-key attacks.
- Step 5 DONE: `catalog_mcp.py --http` (127.0.0.1:8001) with `McpTokenVerifier` + AuthSettings; stdio unchanged
  (agent and evals still use stdio). `tests/test_catalog_http.py` (6): no token / garbage / device-mcp token /
  no-scope / edited token -> 401 with the right logged reason; catalog-mcp token -> sees search_title.
  Learned: stdio trusts the process, HTTP must trust the token; the 401 points to protected-resource metadata
  that names Keycloak (MCP auth discovery). Eval fingerprint `tool_servers` changed (expected).
- Step 6 DONE: `login.py` (authorization code + PKCE, browser login, callback on 127.0.0.1:8765, keeps only the
  refresh token in git-ignored `.login.json`; `--status`, `--logout`). `access_token_for("<server>-mcp")` narrows
  each access token to ONE server via the refresh grant's `scope`. `agent.py`: a SERVERS entry may be a URL
  (CATALOG_MCP_URL in .env) -> HTTP with that server's token; `--tools` shows "http, as <user>".
  Realm: password grant OFF; session idle 1 day, max 7 days. Tests now get tokens through the real login page
  (`tests/kc_browser.py`, a scripted browser). `tests/test_login.py` (9): password grant off, wrong password,
  state mismatch ignored, stolen code useless without verifier (and single-use), logout kills the refresh token,
  each token opens exactly one server, agent reaches catalog over HTTP as the logged-in person. 32 tests total.
  Learned: PKCE binds the code to the program that started the login; `state` binds the answer to the attempt;
  one login, many narrow tokens (down-scoping on refresh).
- Step 7 DONE: `device_mcp.py --http` (127.0.0.1:8002), audience device-mcp, same pattern as catalog.
  `play_title` logs WHO asked (`auth.caller_name()`: token user, or "local (stdio)"), the start of an audit trail.
  Agent: DEVICE_MCP_URL in .env -> device over HTTP. `tests/test_device_http.py` (5, server forced to DRY RUN):
  catalog-mcp token, taste-mcp token, and a token for both other servers are all refused (WRONG_AUDIENCE);
  a device-mcp token plays (dry run) and the log says "requested by mishika". 37 tests total.
  Learned: "the Catalog agent cannot control the TV" is now enforced by the network, not by agent behaviour:
  even a valid login for the same person is refused if the token was issued for another server.
  Verified live: catalog AND device over HTTP as mishika, The Friend played on Netflix (one run).
- Step 8 DONE: `taste_mcp.py --http` (127.0.0.1:8003, HTTP only) on Postgres 16 in Docker (`household-db`,
  host port 5433, volume `household-db-data`; `db/01_init.sql` + `db/schema.psql` run once on an empty volume and
  create databases `household` and `household_test`). Tools: get_my_taste, remember_taste, forget_taste,
  record_watch (AGENT ONLY: hidden from the model; agent code calls it after a real play, not a dry run).
  Two independent layers: (1) no tool has a user argument, the user is the token's `sub`, every query says
  WHERE user_id = caller; (2) Postgres row-level security keyed on `app.user_id` (set per transaction),
  FORCE'd, and the server logs in as `taste_app` (not superuser: superusers bypass RLS; no DELETE on history,
  no DDL). `tests/test_taste_http.py` (13): Mishika cannot see Gaurav's taste/history, naming Gaurav in tool
  arguments does not help, forget only touches your own rows, DB hides other rows without WHERE, no user set =
  no rows, DB refuses a row for someone else, app role cannot DELETE history or DROP, stdio = NOT_LOGGED_IN,
  agent records only real plays. 50 tests total. Mutation-checked: with RLS off only the DB tests fail (the
  server layer holds alone); with the server's WHERE removed nothing fails (RLS holds alone: defence in depth).
  Learned: put the identity in the token, not the arguments; enforce twice; least-privilege DB role.
- Step 9 DONE: per-person TV profiles. device-mcp maps the TOKEN's user to a profile position
  (config NETFLIX_PROFILES gaurav:3,mishika:4; PRIME_PROFILES gaurav:1,mishika:3). The model cannot choose it.
  First live run FAILED. `profile_probe.py` (step-by-step, asks what the screen shows) found: (1) close_app is
  unreliable (once 403 access denied, once OK); (2) after a real close the Netflix picker took 21-22 s (now
  NETFLIX_PICKER_WAIT=25); (3) BOTH pickers stack profiles in a COLUMN; LEFT/RIGHT reach Netflix's edit pencil.
  Keys now: UP x PROFILE_RESET_PRESSES (5), DOWN x (pos-1), ENTER (the picker highlights the LAST USED profile).
  Verified live: Netflix as mishika (GOAT opened in her profile), Prime as gaurav (Masterplan played).
  Learned: blind key sequences encode assumptions about a screen we cannot see; probe the real device step by
  step, and keep each assumption in config.py with a note saying whether it was verified.
  `tests/test_profiles.py` (6, fake TV records keys) + `test_device_http` profile case. 57 tests total.

## After Phase 2: provider-rejected tool calls (2026-10-20)
- Seen live: "The Grand Seduction" (no year) -> the model sent "year": null; GROQ validated the arguments against
  the tool schema and answered HTTP 400 tool_use_failed; agent.py crashed (one real error, wrapped in many
  ExceptionGroup layers by the open MCP HTTP connections).
- Fix in agent.py: catch tool_use_failed in the loop, tell the model what was rejected and why ("never send
  null"), let it retry within MAX_TURNS (same pattern as a failed tool call); other provider errors are NOT
  swallowed. main() unwraps ExceptionGroups (`first_cause`) and prints one clear line for Groq API errors.
  `tests/test_agent_recovery.py` (4, fake Groq replaying the real 400 body). Mutation-checked.
  Learned: a model can fail at the PROVIDER boundary too, not only at the tool boundary; errors there arrive as
  exceptions in your loop, not as tool results.

## Parked from Phase 2 (eval work, one change at a time, measure with --compare)
- 12b: "I don't like horror, and I love Christopher Nolan films" -> must produce TWO remember_taste calls; the
  answer must not claim a preference with no matching call. Seen live: ONE call, answer claimed both (a false
  claim: the trace is the truth, the answer is only a claim). Fix candidates: a prompt line, or remember_taste
  accepting several items.
- Taste tools are not in the evals yet (eval SERVERS are stdio catalog + device only).
- Eval case: a request with no year ("The Grand Seduction") -> count provider rejections in the trace
  (`(rejected by provider)`); the run must still end with a correct search_title.
- `requested` is not `watched`: record_watch runs when commands were SENT, so a title blocked on the TV (seen:
  The Friend on Mishika's Kids profile, maturity rating) still lands in history. Phase 4/5: verify, or label
  history entries "requested".
- Kids profile: Mishika's Netflix profile is a Kids profile. The agent does not know ratings, so it sends titles
  Netflix will refuse. Default when it matters: add maturity rating to search_title + a per-person "kids" flag,
  and have the agent say "not available on <name>'s profile" instead of playing.
- Mixed transports split identity: with taste over HTTP but device over stdio, history went to mishika while the
  TV used the highlighted (gaurav) profile. Default: run all servers over HTTP when logged in, or have the agent
  refuse a TV action over stdio while a login is active.

## Phases 3-5: NOT STARTED

## Known fragile points carried forward
- All waits are fixed sleeps, not checks (matters for Phase 4).
- `close_app` untested on this TV.
- Profiles are picked by position with blind key presses (no way to read the screen); if someone adds or
  reorders profiles, update NETFLIX_PROFILES / PRIME_PROFILES. close_app on this TV is still unverified.
- Access tokens last 5 min and the agent gets them at start: a run longer than that would get 401s mid-run
  (Phase 4: refresh and retry on 401).
- Keycloak dev mode keeps its data inside the container: `docker rm` loses it (the realm file re-creates it).
- Device actions currently run without confirmation (Phase 5 adds approval).
- A retry today could relaunch a title (Phase 4 adds idempotency).