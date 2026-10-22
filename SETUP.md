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
| `catalog_mcp.py --http` | `search_title`: where a title streams in India | its own window | 8001 |
| `device_mcp.py --http` | `tv_status`, `play_title`: controls the TV | its own window | 8002 |
| `taste_mcp.py --http` | `get_my_taste`, `remember_taste`, … | its own window | 8003 |
| `login.py` | Signs one person in (browser), keeps the login in `.login.json` | when needed | 8765 (briefly) |
| `agent.py` | The single-agent concierge: you type a request, it uses the servers | your window | |
| `multi_agent.py` | The team (Phase 3): Supervisor + Catalog agent + Taste agent | your window | |
| Jaeger (optional) | Shows traces: who did what, how long it took | Docker container `jaeger` | 16686 (UI), 4318 |

There are two ways to run the agent:
- **Full mode** (everything over HTTP, with login): per-person data and TV profiles. This guide's default.
- **Quick mode** (no Docker, no login): `agent.py` starts catalog and device itself over stdio. There are no
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
| `GROQ_API_KEY`, `GROQ_MODEL` | Groq key and model. `python agent.py --models` lists the models (baseline: `openai/gpt-oss-120b`) |
| `DEVICE_DRY_RUN` | `1` = never touch the TV (safe); `0` = really control it |
| `PRIME_PRESS_PLAY` | `1` = also press Play on Prime (verified on subscription titles) |

For **full mode**, also add these three lines so the agent always uses the servers over HTTP:
```
CATALOG_MCP_URL=http://127.0.0.1:8001/mcp
DEVICE_MCP_URL=http://127.0.0.1:8002/mcp
TASTE_MCP_URL=http://127.0.0.1:8003/mcp
```
Leave them out (or comment them with `#`) for quick mode.

Everything else has a working default in `config.py`. Change it in `.env` only to override it on this machine.

### 1.4 Pair the TV (once)
```powershell
python cli.py pair
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
python login.py
```
A browser opens. Sign in as `gaurav` / `gaurav-dev-pass` or `mishika` / `mishika-dev-pass`. You stay logged in until
you go a day without using the agent, or for 7 days at most.

---

## 2. Every day (full mode)

### 2.1 Start: the easy way
```powershell
.\start-concierge.ps1
```
It starts both containers, waits for Keycloak, opens one window per server and shows who is logged in.
The first time, Windows may refuse to run a downloaded script. If so, run this once:
`Unblock-File .\start-concierge.ps1`, and if needed `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

### 2.2 Start: by hand (what the script does)
| Window | Commands (after `cd` + `conda activate watch-concierge`) |
|---|---|
| 1 | `docker start keycloak household-db`, wait about 20 s, then `python catalog_mcp.py --http` |
| 2 | `python device_mcp.py --http` (its first line says `LIVE` or `DRY RUN`) |
| 3 | `python taste_mcp.py --http` |
| 4 | `python login.py --status`, then the agent (below) |

### 2.3 Use it (window 4)
```powershell
python agent.py --tools                                    # every tool should say (http, as <person>)
python agent.py "play The Friend 2025"
python agent.py "I don't like horror"
python agent.py "what do you know about my taste?"
python agent.py "is the TV on?"
```
The same requests through the team (Phase 3). Each agent's lines are tagged `[supervisor]`, `[catalog]`, `[taste]`:
```powershell
python multi_agent.py --tools                              # each agent's toolbox: no overlap
python multi_agent.py "play The Friend 2025"
python multi_agent.py "I don't like horror, and I love Christopher Nolan films"
python multi_agent.py "play tum hi ho by arijit singh"           # songs: YouTube (search by name)
```
The TV uses the logged-in person's profile (Netflix: Gaurav 3, Mishika 4; Prime: Gaurav 1, Mishika 3).
Mishika's Netflix profile is a Kids profile, so Netflix blocks mature titles there.

### 2.4 Switch person
```powershell
python login.py --logout
python login.py                # sign in as the other person
```

### 2.5 Stop
Close the three server windows (or press Ctrl+C in each), then:
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
python login.py                          # the old login ended with the old Keycloak
```
Restart `taste_mcp.py --http`. Then each person turns sharing on ONCE, for themselves (off by default; only likes
and dislikes are ever shared, never watch history):
```powershell
python login.py --logout; python login.py                      # sign in as mishika
python multi_agent.py "share my taste with the household"
python login.py --logout; python login.py                      # back to gaurav
python multi_agent.py "share my taste with the household"
python multi_agent.py "me and Mishika want to watch something tonight"
```
Every cross-person read is in the audit log:
`docker exec household-db psql -U postgres -d household -c "select * from household_reads order by id desc limit 10"`

## 3. Quick mode (no Docker, no login)
Comment out the three `*_MCP_URL` lines in `.env`, then run `python agent.py "play The Friend 2025"`. The agent starts
catalog and device itself. There are no taste tools, and the TV uses the highlighted profile.
The non-agent CLI also works without anything running: `python cli.py watch "The Friend"`.

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
python tests\test_multi_agent.py    # 11  the team: handoffs, each agent's tools and tokens, whose taste
python tests\test_tracing.py        #  7  spans: one trace per request, nesting, no tokens in spans
python tests\test_household.py      #  9  household read: permission, consent, no history, audit, agents
```
Or all at once: `python -m pytest tests` (pytest comes with `requirements.txt`). Use `python -m pytest`,
not plain `pytest`: plain `pytest` can pick up Anaconda's base Python, which has none of the project's
packages (`No module named 'groq'`). If Keycloak or Postgres is down, those tests show SKIP.

Evals (quick mode, real Groq model, fake TV and catalog):
```powershell
python evals\selftest.py
python evals\run_evals.py --reps 3 --label myrun
python evals\run_evals.py --compare evals\results\<a>.json evals\results\<b>.json
```

---

## 5. Troubleshooting
| You see | Meaning and fix |
|---|---|
| `Login problem: Nobody is logged in` | Run `python login.py`. |
| `Login problem: ... has expired or was ended` | The login timed out (1 day idle). Run `python login.py`. |
| `Keycloak not reachable` / tests SKIP | `docker start keycloak`, wait about 20 s. |
| `No module named 'groq'` (or `mcp`) under pytest | Plain `pytest` ran from Anaconda base. Use `python -m pytest tests` in the `watch-concierge` env. |
| `Port 800x is already in use` | That server is already running in another window. Close it or reuse it. |
| `relation "taste_items" does not exist` | The DB setup didn't run. `docker rm -f household-db; docker volume rm household-db-data`, then 1.5 again. **This deletes taste data.** |
| `NOT_LOGGED_IN` from a taste tool | Taste only works over HTTP. Set `TASTE_MCP_URL` and start `taste_mcp.py --http`. |
| `"profile": "... (person unknown)"` | The device server ran over stdio (no token). Set `DEVICE_MCP_URL` and start `device_mcp.py --http`. |
| Wrong Netflix/Prime profile | Watch `python profile_probe.py` step by step (stop `device_mcp.py` first). Positions are in `config.py` (`NETFLIX_PROFILES`, `PRIME_PROFILES`). |
| Picker keys pressed too early | Raise `NETFLIX_PICKER_WAIT` (default 25) or `PRIME_LINK_TO_KEY_WAIT` in `.env`. |
| `tracing: could NOT reach http://localhost:4318` | Jaeger is not running: `docker start jaeger` (or run the 1.5 command). |
| `Groq REJECTED the model's tool call` | Normal. The model sent bad arguments; the agent tells it why and it retries. |
| `TV_UNREACHABLE` | The TV is off or asleep beyond wake-on-LAN, or `TV_IP` is wrong. Check `logs\device_mcp.log`. |
| Nothing happens on the TV, result says `dry_run` | `DEVICE_DRY_RUN=1` in `.env`. Set `0`, then restart `device_mcp.py`. |

Logs: `logs\device_mcp.log` (every TV action, with who asked), `logs\test_*.log` (servers started by the tests).

## 6. Reset (development)
```powershell
docker rm -f keycloak                                    # Keycloak: re-created from the realm file by 1.5
docker rm -f household-db; docker volume rm household-db-data   # Postgres: DELETES all taste data
Remove-Item .login.json                                  # forget the login on this PC
```