# Handoff: Phase 5 closed, ideas 1 and 2a done; next = the cloud + home plan (C0)

Attach this file, `PLAN.md`, `SETUP.md` and the project zip to a new chat. Say: "Continue the Household Watch
Concierge from HANDOFF.md. Start the cloud + home plan at C0."
**Do not put secrets in the zip**: leave out `.env`, `.webos_key`, `.login.json` and `.state\` (the 2026-10-24 zip
contained them; rotate the Groq, RapidAPI and TMDB keys if that zip went anywhere else).
How to start and run everything: `SETUP.md`. Every command: `COMMANDS.ps1`. Request flows: `FLOWS.md`.
Full history and every decision: `PLAN.md` (sections "Phase 5", "Idea 1", "Idea 2", "Cloud + home plan").
Architecture diagram (interactive; private to Gaurav): https://claude.ai/artifact/6mnd6SJUkWvue9kvEX44yq
(its views go up to P4; Phase 5 and ideas 1 / 2a are NOT drawn yet: FLOWS.md describes them).

## Who and why (unchanged)
- Gaurav, India, Windows + PowerShell, conda env `watch-concierge`. LG webOS TV on the home LAN.
- **Primary goal: learn agentic concepts.** Household use is secondary. Wants it QUICK and focused on agent
  concepts; park business details in PLAN.md.
- Working style that works: small exact changes; files sent INDIVIDUALLY (not zipped) with a table of where each
  goes; CRLF line endings; explain step by step (what each change is for, the CONCEPT by name), short on test
  detail, clear on how the flow works. Ask before overwriting a file he may have edited. New dependencies go into
  requirements.txt in the same step (pinned to a version at least two weeks old). **Gaurav commits himself: never
  commit, push, or suggest git steps.**
- Teaching loop: probe the real library first, build a thin piece, run a visible trace, name the concept, a test
  that fails in the RIGHT way, mutation-check it (break the code on purpose; a test must fail). Measure with evals.
- LLM: Groq. `GROQ_MODEL=openai/gpt-oss-120b`; optional `ROUTER_MODEL`. No Anthropic key.
- Versions: mcp 2.3.0, LangGraph 1.2 (+ langgraph-checkpoint-sqlite 3.1), a2a-sdk 1.2.0 (A2A spec 1.0),
  Keycloak 26.0 (Docker), Postgres 16 (Docker), Jaeger 2.22.0, OpenTelemetry 1.45.1, PyJWT, psycopg 3, pytest >= 8.
  Run tests with `python -m pytest tests`. The assistant tests in its own sandbox (no Groq key there: evals there run
  only the 0-model-call cases; no Keycloak/Postgres unless set up, so 54 HTTP tests skip there).

## How to run a request
| Entry | What it is | Model calls (typical) |
|---|---|---|
| `python -m concierge.graph "..."` | **day to day**: router + workflows + skill agents + team, durable | 0 for play/song/status |
| `python -m concierge.graph --chat` | conversations: answers continue a question; history; "play the first one" | 0 per answer; ~2 for a reference |
| `python -m concierge.graph --pending / --reply <id> "..." / --resume <id>` | WAITING (answer it) vs crashed (resume it) | |
| `python -m concierge.team "..."` | the team alone (the graph's fallback node) | 4-6 |
| `python -m servers.catalog_agent.server` | OPTIONAL: the Catalog agent over A2A (with `CATALOG_AGENT_URL`) | its own |
| `baselines/` | single agent, hand-written router: eval baselines only | |
To watch a movie: `.env` `DEVICE_DRY_RUN=0`, `.\start-concierge.ps1`, `python -m common.login --status`, then
`python -m concierge.graph --chat`. The A2A catalog-agent is NOT needed (a "play X" is a code workflow that
searches catalog-mcp directly; the in-process Catalog agent serves the team).

## What was built since the last handoff (2026-10-24 / 25)
- **Phase 5, Step 1: prompt injection, PROVENANCE guard** (`engine.SEARCHED_IDS`). McpToolbox.call records, BY CODE,
  the (service, id) pairs search_title / search_youtube returned; the agent loop BLOCKS a model's play_title whose
  pair is not among them. Eval #16 (a hostile YouTube title asking to play another id): team, gpt-oss-120b, 10 runs:
  the model obeyed 2/10, the guard blocked both; graph: 0 model calls. `run_evals --guard off` = evals only.
- **Phase 5, Step 2: the Taste agent hears only the person** (team.ask_taste_tool passes the request by code;
  the tool has no argument). Phase 5 then CLOSED (rest = product hardening, not concepts).
- **Idea 1: conversations.** 1a pause/resume: PLAY returns an `Ask` (question + options from the search); the `ask`
  node holds only `interrupt()`; `choose` matches the reply by code (`pick_option`); on resume the paused node
  re-runs from its first line. 1b `turn_kind`: is a chat line an answer or a new request (the routing rules judge;
  a new request closes the open question with "cancel"). 1c message memory: one thread per conversation, each
  request a turn (request id `<thread>#<turn>`), `history` (words + final answers, last 3 turns, a reducer trims,
  never tool results) written by a `remember` node at the end of every turn, given to the team and Consensus;
  `known_ids` (code-saved searched ids) carry provenance across turns; `router.refers_back` sends "play the first
  one" to the team. Verified live (1a): "play game changer" -> "netflix" played the 2021 one on the real TV.
- **Idea 2, step 2a: the Catalog agent over A2A.** `servers/catalog_agent/server.py` (Agent Card, executor:
  message -> task WORKING -> COMPLETED/FAILED, artifacts "answer" (model text) + "searched_ids" (code data));
  `concierge/remote.py` (the client); `team.ask_catalog_tool` goes remote when `CATALOG_AGENT_URL` is set (the
  Supervisor sees the same tool). Lesson: trust does not cross the network by itself (ids travel in the data
  artifact). 2b/2c are now OPTIONAL; the "on behalf of" identity part moved into the cloud plan (C2).
  start-concierge.ps1 opens the catalog-agent window only when CATALOG_AGENT_URL is set (not yet run on Windows).

## Key design facts (cumulative)
- Phase 3: Agent loop (model proposes, code decides; repeats blocked), McpToolbox (one token per server audience,
  only offered tools run), Supervisor + Catalog / Taste / Consensus agents, skills (`skills/*.md`), router (rules,
  then ROUTER_MODEL, else FALLBACK), workflows (policies as code, 0 calls), household read (scope + consent, audited).
- Phase 4: graph nodes route, play, song, status, taste, suggest, fallback + (idea 1) ask, choose, remember; every
  END edge goes through `remember`. Durable AsyncSqliteSaver `.state/checkpoints.sqlite`, durability="sync"; a node
  runs AT LEAST ONCE (hence idempotency keys from REQUEST_ID; device-mcp IdempotencyStore). retry/breaker/cache in
  lookup.py; RetryPolicy on code nodes only. Non-durable runs (evals/tests) use an InMemorySaver (interrupt needs one).
- State: request, route, answer, question/reply/asked (a paused question), turn, history (reducer keep_recent),
  known_ids. All plain data (checkpointed).
- Phase 5: SEARCHED_IDS opened by the outermost Agent.loop (and by every graph node, seeded with known_ids); shared by
  nested agents and check_titles; an A2A remote agent always starts its OWN set.

## Tests, evals, drills
- 192 tests (`python -m pytest tests`; Keycloak + Postgres up, ALL server windows CLOSED: otherwise "Port 800x is
  already in use"). New: test_injection 7, test_conversation 16, test_a2a 6. Sandbox: 138 offline pass, 54 HTTP skip.
  2026-10-25 on Gaurav's PC: 192 passed (Keycloak + Postgres up), selftest ok, 5 drills pass. C0 tests: DONE.
- Evals: 16 cases (`--agent graph|team|routed|single`, `--guard off`). Graph 48/48 at 0.4 calls/run (2026-10-24).
- `python evals\drills.py`: 5 failure drills pass. `python evals\selftest.py`: all ok.

## NEXT: the cloud + home plan (PLAN.md "Cloud + home plan"), minimal application changes
C0 green baseline (tests DONE 2026-10-25: 192 passed; still open: SETUP 4b live checks) -> C1 web page + API around
graph.chat_turn on the PC (phone on home Wi-Fi, browser speech-to-text) -> C2 per-user identity (token from the
page, token exchange per server; Keycloak >= 26.2) -> C3 device-mcp on an always-on home box behind an outbound
tunnel -> C4 the rest on a small cloud VM (Docker Compose; Keycloak production mode with HTTPS).
WORKING NOW (2026-10-25): phone -> Tailscale -> SSH -> this PC -> chat.ps1 -> the TV (a YouTube song played from
the iPhone). No code changes. Firewall rule sshd-tailscale; keep the PC from sleeping. Likely direction: all at home
(free), C1 = a web page served from the box instead of the SSH app.
Decisions still Gaurav's: home box, tunnel (suggested Tailscale), cloud host (check current prices then), web app.

## Known fragile points carried forward
- Profiles picked by position with blind key presses; picker/title waits fixed (the TV cannot report them).
- close_app on this TV is unreliable (once 403). Wake-on-LAN works from standby, not from fully off.
- Keycloak dev mode keeps data in the container (realm file re-creates it; logins are lost).
- Device actions run without confirmation (decided: no approval step).
- Not yet verified live: SETUP 4b checks, step 2's empty-argument taste tool with Groq, 1b/1c chat with the real
  model, the A2A catalog-agent on Windows, start-concierge.ps1's new catalog-agent block.
- In --chat, a new request no routing rule claims ("what about something funny?") is read as an answer.
- The A2A catalog-agent has no login (127.0.0.1 only) and its trace is separate from the caller's.
