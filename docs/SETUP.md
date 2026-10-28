# Household Watch Concierge: setup and running

Windows + PowerShell. Every command runs from the project folder:
```powershell
cd "D:\Gaurav Balyan\AI Agents\Watch Concierge"
conda activate watch-concierge
```

## What runs where

| Piece | What it does | Where it runs | Port |
|---|---|---|---|
| Keycloak | Login. Issues the tokens the servers check | Docker container `keycloak` | 8080 |
| Postgres | Each person's likes, dislikes and watch history | Docker container `household-db` | 5433 |
| `python -m servers.catalog.server --http` | `search_title`: where a title streams in India | its own window | 8001 |
| `python -m servers.device.server --http` | `tv_status`, `play_title`: controls the TV | its own window | 8002 |
| `python -m servers.taste.server --http` | `get_my_taste`, `remember_taste`, … | its own window | 8003 |
| `python -m servers.catalog_agent.server` | OPTIONAL (A2A, idea 2): the Catalog agent in its own process. Only with `CATALOG_AGENT_URL` set; not needed to watch anything | its own window | 8004 |
| `common/login.py` | Signs one person in (browser), keeps the login in `.login.json` | when needed | 8765 (briefly) |
| `concierge/team.py` | The team (Phase 3): Supervisor + specialists; the graph's fallback | your window | |
| `baselines/` | The single agent and the hand-written router: kept only as eval baselines | your window | |
| `concierge/graph.py` | **Use this one** (Phase 4): the router + workflows + skills + team as a durable LangGraph graph | your window | |
| Jaeger (optional) | Shows traces: who did what, how long it took | Docker container `jaeger` | 16686 (UI), 4318 |

There are two ways to run the agent:
- **Full mode** (everything over HTTP, with login): per-person data and TV profiles. This guide's default.
- **Quick mode** (no Docker, no login): the agent starts catalog and device itself over stdio. There are no
  taste tools, and the TV uses whichever profile is highlighted. Use it for a quick test, or when Docker isn't running.

---

## 1. One-time setup

### 1.1 Prerequisites
- Anaconda (or Miniconda), with PowerShell enabled for conda (`conda init powershell`, once).
- **Docker Desktop**, running. Check: `docker info` prints a server version.
- The LG TV and this PC on the same home network. The TV's **Mobile TV On / Wake on LAN** setting is on.

### 1.2 Python environment
```powershell
conda create -n watch-concierge python=3.12 -y
conda activate watch-concierge
pip install -r requirements.txt
```

### 1.3 Settings (`.env`)
```powershell
Copy-Item .env.example .env
notepad .env
```
Fill in at least:
| Key | What |
|---|---|
| `TV_IP`, `TV_MAC` | The TV's address and MAC address. Find them in the TV's network settings, or on your router. |
| `RAPIDAPI_KEY` | Streaming Availability API key (RapidAPI) |
| `GROQ_API_KEY`, `GROQ_MODEL` | Groq key and model. `python -m tools.groq_models` lists the models (baseline: `openai/gpt-oss-120b`) |
| `DEVICE_DRY_RUN` | `1` = never touch the TV (safe); `0` = really control it |
| `PRIME_PRESS_PLAY` | `1` = also press Play on Prime (verified on subscription titles) |

For **full mode**, also add these three lines so the agent always uses the servers over HTTP:
```
CATALOG_MCP_URL=http://127.0.0.1:8001/mcp
DEVICE_MCP_URL=http://127.0.0.1:8002/mcp
TASTE_MCP_URL=http://127.0.0.1:8003/mcp
```
Leave them out (or comment them with `#`) for quick mode.

Everything else has a working default in `common/config.py`. Change it in `.env` only to override it on this machine.

### 1.4 Pair the TV (once)
```powershell
python -m tools.cli pair
```
Accept the prompt on the TV. The key is saved in `.webos_key`.

### 1.5 Create the two containers (once)
**Keycloak.** It imports `keycloak\household-realm.json`: users `gaurav` and `mishika`, passwords `<name>-dev-pass`.
```powershell
docker run -d --name keycloak -p 8080:8080 `
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin `
  -v "${PWD}\keycloak:/opt/keycloak/data/import" `
  quay.io/keycloak/keycloak:26.0 start-dev --import-realm
```
**Postgres.** On the first start it runs `db\01_init.sql` and `db\schema.psql`. That creates the role `taste_app` and
the databases `household` (real data) and `household_test` (tests only).
```powershell
docker run -d --name household-db -p 5433:5432 `
  -e POSTGRES_PASSWORD=postgres-dev-pass -e POSTGRES_DB=household `
  -v "${PWD}\db:/docker-entrypoint-initdb.d" -v household-db-data:/var/lib/postgresql/data `
  postgres:16
```
Check both, about 30 seconds later:
```powershell
docker exec household-db psql -U postgres -d household -c "\dt"     # lists taste_items, watch_history
irm http://localhost:8080/realms/household/.well-known/openid-configuration | select issuer
```

**Jaeger (optional, for tracing).** Only needed if you set `TRACING=jaeger` (see 2.6).
```powershell
docker run -d --name jaeger -p 16686:16686 -p 4318:4318 cr.jaegertracing.io/jaegertracing/jaeger:2.22.0
```
Check: open http://localhost:16686 (the Jaeger page loads). Traces are kept in memory: `docker rm` clears them.

### 1.6 First login
```powershell
python -m common.login
```
A browser opens. Sign in as `gaurav` / `gaurav-dev-pass` or `mishika` / `mishika-dev-pass`. You stay logged in until
you go a day without using the agent, or for 7 days at most.

---

## 2. Every day (full mode)

### 2.1 Start: the easy way
```powershell
.\start-concierge.ps1
```
It starts both containers (and Jaeger if it exists), waits for Keycloak, opens one window per server and shows
who is logged in. With `CATALOG_AGENT_URL` set in `.env` it also opens the catalog-agent window (A2A, optional).
The first time, Windows may refuse to run a downloaded script. If so, run this once:
`Unblock-File .\start-concierge.ps1`, and if needed `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### 2.2 Start: by hand (what the script does)
| Window | Commands (after `cd` + `conda activate watch-concierge`) |
|---|---|
| 1 | `docker start keycloak household-db`, wait about 20 s, then `python -m servers.catalog.server --http` |
| 2 | `python -m servers.device.server --http` (its first line says `LIVE` or `DRY RUN`) |
| 3 | `python -m servers.taste.server --http` |
| 4 | `python -m common.login --status`, then the agent (below) |

### 2.3 Use it (window 4)
```powershell
python -m baselines.single_agent --tools                                    # every tool should say (http, as <person>)
python -m baselines.single_agent "play The Friend 2025"
python -m baselines.single_agent "I don't like horror"
python -m baselines.single_agent "what do you know about my taste?"
python -m baselines.single_agent "is the TV on?"
```
The same requests through the team (Phase 3). Each agent's lines are tagged `[supervisor]`, `[catalog]`, `[taste]`:
```powershell
python -m concierge.team --tools                              # each agent's toolbox: no overlap
python -m concierge.team "play The Friend 2025"
python -m concierge.team "I don't like horror, and I love Christopher Nolan films"
python -m concierge.team "play tum hi ho by arijit singh"           # songs: YouTube (search by name)
```
The ROUTED concierge (Phase 3, Step 7): a router picks the cheapest path. Play, song and "is the TV on" run as
code (0 model calls); taste and suggestions run as skills (`concierge/skills/*.md`); anything else goes to the team.
The first line printed says which route was taken and why:
```powershell
python -m baselines.routed "play The Friend 2025"
python -m baselines.routed "me and Mishika want to watch something tonight"
```
**Day to day, use `concierge/graph.py` (Phase 4)**: the same routes as `baselines/routed.py`, but DURABLE. Its first line prints a
request id; if the run dies (crash, closed window, PC asleep), finish it from where it stopped. The play is never
sent twice: device-mcp recognises the request's idempotency key.
```powershell
python -m concierge.graph "play The Friend 2025"         # first line: [graph] request id 20261023-171200-ab12cd
python -m concierge.graph --pending                      # requests that did not finish
python -m concierge.graph --resume 20261023-171200-ab12cd
python -m concierge.graph --draw                         # the graph's shape (Mermaid text: paste into mermaid.live)
```
**Conversations (idea 1):** `python -m concierge.graph --chat` keeps one conversation going. When it asks
("Netflix or Prime?"), your next line answers it ("prime", "the 2021 one", "2", "cancel"); a new request instead
("actually, is the tv on?") closes the question first. After a suggestion, "play the first one" works: the team
reads the last 3 turns. Outside chat, a paused request shows as `WAITING` in `--pending`; answer it with
`python -m concierge.graph --reply <id> "prime"`.
Phase 4 keeps small state files in `.state\` (git-ignored): checkpoints, the TV's idempotency records, the
catalog cache and circuit breaker. Deleting the folder is safe (you lose unfinished requests and the cache).

The TV uses the logged-in person's profile (Netflix: Gaurav 3, Mishika 4; Prime: Gaurav 1, Mishika 3).
Mishika's Netflix profile is a Kids profile, so Netflix blocks mature titles there.

### 2.4 Switch person
```powershell
python -m common.login --logout
python -m common.login                # sign in as the other person
```

### 2.5 Stop
Close the server windows (or press Ctrl+C in each), then:
```powershell
docker stop keycloak household-db jaeger
```
Your data stays. `docker start` brings it back.

---

### 2.6 Tracing (optional)
Add to `.env` (then restart the three servers so they read it):
```
TRACING=print,jaeger
```
- `print`: after each run the agent prints the span tree (who did what, nested, with durations).
- `jaeger`: the agent AND the servers send spans to Jaeger (`docker start jaeger`). One request = one trace,
  across processes: open http://localhost:16686, pick the service `watch-concierge (team)`, then **Find Traces**.
- Leave `TRACING` empty to turn tracing off. Tokens are never recorded. Tool results only with `TRACE_CONTENT=1`.

### 2.7 Phase 3, Step 5: household sharing (once, after updating the files)
The realm gets a new permission and the database two new tables. Your taste data stays.
```powershell
docker rm -f keycloak                    # then run the Keycloak command from 1.5 again (re-imports the realm)
Get-Content db\household.psql | docker exec -i household-db psql -U postgres -d household
Get-Content db\household.psql | docker exec -i household-db psql -U postgres -d household_test
python -m common.login                          # the old login ended with the old Keycloak
```
Restart `servers/taste/server.py --http`. Then each person turns sharing on ONCE, for themselves (off by default; only likes
and dislikes are ever shared, never watch history):
```powershell
python -m common.login --logout; python -m common.login                      # sign in as mishika
python -m concierge.team "share my taste with the household"
python -m common.login --logout; python -m common.login                      # back to gaurav
python -m concierge.team "share my taste with the household"
python -m concierge.team "me and Mishika want to watch something tonight"
```
Every cross-person read is in the audit log:
`docker exec household-db psql -U postgres -d household -c "select * from household_reads order by id desc limit 10"`

## 3. Quick mode (no Docker, no login)
Comment out the three `*_MCP_URL` lines in `.env`, then run `python -m baselines.single_agent "play The Friend 2025"`. The agent starts
catalog and device itself. There are no taste tools, and the TV uses the highlighted profile.
`python -m concierge.graph "play The Friend 2025"` also works in quick mode (no login: no taste, highlighted profile).
`tools/cli.py` is only for pairing and tuning the TV (`python -m tools.cli` lists its commands).

---

## 4. Tests and evals
Full suite (Keycloak and Postgres running; the servers are started by the tests themselves, in DRY RUN).
**Close your three server windows first**: the tests start their own copies on the same ports (using the test
database), and stop with `Port 800x is already in use` if yours are running.
```powershell
python tests\test_auth.py          # 17  tokens: audience, expiry, forged and edited tokens
python tests\test_catalog_http.py  #  6  catalog refuses wrong tokens
python tests\test_login.py         #  9  PKCE login, logout, one token per server
python tests\test_device_http.py   #  6  a catalog token cannot control the TV; profiles per person
python tests\test_taste_http.py    # 13  Mishika cannot read Gaurav's data (server + database)
python tests\test_profiles.py      #  6  offline: exact TV key presses per person
python tests\test_agent_recovery.py #  4  offline: Groq-rejected tool calls do not crash the agent
python tests\test_agent_loop.py     #  7  offline: record_watch is wired in by code, never by the model
python tests\test_multi_agent.py    # 15  the team: handoffs, each agent's tools and tokens, whose taste
python tests\test_tracing.py        #  7  spans: one trace per request, nesting, no tokens in spans
python tests\test_household.py      # 10  household read: permission, consent, no history, audit, agents
python tests\test_routing.py        # 33  offline: router, workflow policies (0 model calls), skills
python tests\test_graph.py          # 11  offline: the graph = baselines/routed.py (parity), its edges, node spans
python tests\test_resilience.py     # 21  offline: crash + resume, idempotency, retry, breaker, cache, TV, 401
python tests\test_injection.py      #  7  offline: prompt injection: an id no search returned is blocked; taste hears only you
python tests\test_conversation.py   # 16  offline: pause/resume on a question, answer or new request, message memory
python tests\test_a2a.py            #  6  offline: the Catalog agent over A2A (card, task, provenance across the hop)
```
Or all at once (202 tests, incl. test_telegram.py's 8): `python -m pytest tests` (pytest comes with `requirements.txt`). Use `python -m pytest`,
not plain `pytest`: plain `pytest` can pick up Anaconda's base Python, which has none of the project's
packages (`No module named 'groq'`). If Keycloak or Postgres is down, those tests show SKIP.

Evals (quick mode, real Groq model, fake TV and catalog):
```powershell
python evals\selftest.py
python evals\run_evals.py --reps 3 --label myrun
python evals\run_evals.py --compare evals\results\<a>.json evals\results\<b>.json
python evals\run_evals.py --agent team --reps 3 --label team    # Phase 3: the team, same cases
python evals\run_evals.py --agent routed --reps 3 --label routed  # Phase 3, Step 7: router + workflows
python evals\run_evals.py --agent graph --reps 3 --label graph    # Phase 4: the graph (the day-to-day entry)
```
Failure drills (Phase 4; no Groq, no TV, no RapidAPI, about a minute): break one thing on purpose and see what the
person is told, how long it took, and that nothing launched twice.
```powershell
python evals\drills.py
```
`--agent team` runs the Supervisor + Catalog agent on the same fake servers (no taste, no login). Each run also
reports MODEL CALLS, so the team's cost shows next to its pass rate. A team run makes roughly twice as many Groq
calls: add `--delay 2` if you hit rate limits.

---

## 4b. Live checks on the real TV (Phase 4)
Tests, evals and drills use a fake TV. These checks are the part only your living room can do. Run them in
order; each says what to look at. Window 1 = `.\start-concierge.ps1` (servers), window 4 = where you type.

**Prepare (once)**
```powershell
docker start jaeger                                   # start-concierge.ps1 does not start it
python -m common.login                                       # sign in as gaurav
```
In `.env`: `TRACING=print,jaeger`. Start with `DEVICE_DRY_RUN=1`. Close and reopen the device window after any
`.env` change (the servers read `.env` at start).

| # | Do | Expect | Look at |
|---|---|---|---|
| 1 | `python -m concierge.graph "play The Friend 2025"` (dry run) | "only a simulation", 0 model calls | printed tree: `graph` > `node route` > `node play` > `execute_tool search_title`, `play_title`; `connect` spans show `aud=catalog-mcp` / `aud=device-mcp` |
| 2 | Same request again | same answer | Jaeger, service `catalog-mcp`: the 2nd search span has `catalog.source=cache` (no RapidAPI call) |
| 3 | Set `DEVICE_DRY_RUN=0`, restart the device window. **TV ON.** `python -m concierge.graph "play The Friend 2025"` | the title opens on **Gaurav's** Netflix profile | trace: `tv network check` with `tv.answered_after_s` ~0; **no** `tv wait after wake` span. Each `tv wait netflix ...` span has `tv.ready_after_s`: write these down |
| 4 | **TV in standby** (remote off, wait 1 min). Same request | the TV wakes and the title opens | `tv.answered_after_s` must be under 25. If you get "no answer within 25s", raise `TV_WAKE_WINDOW` in `.env` |
| 5 | **TV fully off** (power at the wall) | in about 25 s: "The TV didn't respond ... Nothing was launched" | the `tv network check` span ends with `tv.outcome=no answer` |
| 6 | **Crash drill, for real** (TV on): `$env:CHAOS_CRASH="play:after"`, then `python -m concierge.graph "play The Friend 2025"` | the title opens, then `[chaos] ... killing the process` | note the request id from the first line |
| 7 | `$env:CHAOS_CRASH=""` then `python -m concierge.graph --pending`, then `python -m concierge.graph --resume <id>` | `--pending` lists it with `next: play`; resume answers "...not sent a second time"; **the TV does NOT relaunch** | `logs\device_mcp.log`: `play_title REPEAT of key ... not launching again` |
| 8 | **Close the window** mid-play (a Prime title takes ~45 s): start it, close the window after ~10 s; **wait a minute** (the device server may still be finishing the play); new window: `--pending`, `--resume <id>` | EITHER "...not sent a second time" OR "UNKNOWN_STATE ... check the TV". **Never** a second launch | both answers are correct: which one depends on whether the play had finished when the window closed. Resumed too soon? "Another TV action is still running" (BUSY): nothing new starts, and that request ends there |
| 9 | `python -m concierge.graph "play tum hi ho by arijit singh"` and `python -m concierge.graph "is the tv on?"` | song plays; status answers | `node song` / `node status`, 0 `chat` spans |
| 10 | `python -m common.login` as **mishika**, `python -m concierge.graph "play The Friend 2025"` | **Mishika's** profile (Netflix Kids profile: mature titles blocked there) | `connect` spans `as mishika`; then log in as gaurav again |
| 11 | `python -m concierge.graph "I like comedy, can you suggest a few titles"` | the preference is saved, then suggestions (only confirmed titles) | tree: `node route` > `node taste` > `node suggest` (two agents, in that order) |

Optional, token renewal (Step 8): in the Keycloak admin console (http://localhost:8080, admin/admin), realm
`household` > Realm settings > Tokens > Access Token Lifespan = **1 minute**. Run check 11 (several model calls,
often > 30 s). Expect no 401 and no "Login problem". Set it back to **5 minutes** afterwards.

When done: set `DEVICE_DRY_RUN` the way you want it day to day, and tell the assistant the numbers from 3 and 4
(the `tv.ready_after_s` values): they decide whether the fixed waits can be shortened.

**Reading the wait timings without Jaeger:** every TV wait is also a line in the device log, e.g.
`wait netflix picker: 25s fixed, app in front after 4.0s`. After a few real plays:
```powershell
Select-String -Path logs\device_mcp.log -Pattern "  wait " | Select-Object -Last 20
```
A fixed wait much longer than "app in front after" is a candidate to shorten in `.env` (keep a margin: the app
being in front does not mean its picker is drawn yet).

## 5. Troubleshooting
| You see | Meaning and fix |
|---|---|
| `Login problem: Nobody is logged in` | Run `python -m common.login`. |
| `Login problem: ... has expired or was ended` | The login timed out (1 day idle). Run `python -m common.login`. |
| `Keycloak not reachable` / tests SKIP | `docker start keycloak`, wait about 20 s. |
| `No module named 'groq'` (or `mcp`) under pytest | Plain `pytest` ran from Anaconda base. Use `python -m pytest tests` in the `watch-concierge` env. |
| `Port 800x is already in use` | That server is already running in another window. Close it or reuse it. |
| `relation "taste_items" does not exist` | The DB setup didn't run. `docker rm -f household-db; docker volume rm household-db-data`, then 1.5 again. **This deletes taste data.** |
| `NOT_LOGGED_IN` from a taste tool | Taste only works over HTTP. Set `TASTE_MCP_URL` and start `servers/taste/server.py --http`. |
| `"profile": "... (person unknown)"` | The device server ran over stdio (no token). Set `DEVICE_MCP_URL` and start `servers/device/server.py --http`. |
| Wrong Netflix/Prime profile | Watch `python -m tools.profile_probe` step by step (stop `servers/device/server.py` first). Positions are in `common/config.py` (`NETFLIX_PROFILES`, `PRIME_PROFILES`). |
| Picker keys pressed too early | Raise `NETFLIX_PICKER_WAIT` (default 25) or `PRIME_LINK_TO_KEY_WAIT` in `.env`. |
| `tracing: could NOT reach http://localhost:4318` | Jaeger is not running: `docker start jaeger` (or run the 1.5 command). |
| `Groq REJECTED the model's tool call` | Normal. The model sent bad arguments; the agent tells it why and it retries. |
| `TV_UNREACHABLE` | The TV is off or asleep beyond wake-on-LAN, or `TV_IP` is wrong. Check `logs\device_mcp.log`. |
| `TV_UNREACHABLE: no answer on the network within 25s` but the TV was in standby | Phase 4 gives up early. Raise `TV_WAKE_WINDOW` in `.env` (the trace's `tv network check` span shows how long it took when it worked). |
| `CATALOG_UNAVAILABLE ... paused for Ns` | The catalog API failed several times in a row, so the circuit breaker paused it (`CATALOG_BREAKER_RESET`, default 60 s). Wait, or delete `.state\catalog.sqlite`. |
| `UNKNOWN_STATE: an earlier attempt of this same request started...` | A resumed request whose play was cut off mid-action. Look at the TV; ask again (a NEW request) if nothing opened. |
| `[chaos] CHAOS_CRASH=...` | A failure drill setting is in your environment or `.env`. Remove `CHAOS_CRASH`. |
| Nothing happens on the TV, result says `dry_run` | `DEVICE_DRY_RUN=1` in `.env`. Set `0`, then restart `servers/device/server.py`. |

Logs: `logs\device_mcp.log` (every TV action, with who asked), `logs\test_*.log` (servers started by the tests).

## 6. Reset (development)
```powershell
docker rm -f keycloak                                    # Keycloak: re-created from the realm file by 1.5
docker rm -f household-db; docker volume rm household-db-data   # Postgres: DELETES all taste data
Remove-Item .login.json                                  # forget the login on this PC
```