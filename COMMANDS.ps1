return   # CHEAT SHEET, not a script: this line stops it if run by accident. Copy the lines you need.
# =====================================================================================================
# Household Watch Concierge: commands to launch and use the agents (Windows PowerShell)
# Details: SETUP.md. Every command runs from the project folder, with the conda environment active.
# =====================================================================================================

cd "D:\Gaurav Balyan\AI Agents\Watch Concierge"
conda activate watch-concierge


# -----------------------------------------------------------------------------------------------------
# 1. START EVERYTHING (full mode: login, per-person profiles and taste)
# -----------------------------------------------------------------------------------------------------
.\start-concierge.ps1               # starts Keycloak + Postgres (Docker), opens 3 server windows:
                                    #   catalog-mcp :8001, device-mcp :8002 (first line: LIVE or DRY RUN), taste-mcp :8003
docker start jaeger                 # optional: trace viewer (the script does not start it) -> http://localhost:16686
python -m common.login --status            # who is logged in, and for how long


# -----------------------------------------------------------------------------------------------------
# 2. LOG IN / SWITCH PERSON (the TV profile and taste follow the logged-in person)
# -----------------------------------------------------------------------------------------------------
python -m common.login                     # browser opens: gaurav / gaurav-dev-pass  or  mishika / mishika-dev-pass
python -m common.login --logout            # then python -m common.login again as the other person


# -----------------------------------------------------------------------------------------------------
# 3. USE IT, DAY TO DAY: concierge/graph.py (router -> the cheapest path; durable)
#    The first line printed is the request id; the [router] line says which route was taken and why.
# -----------------------------------------------------------------------------------------------------
python -m concierge.graph "play The Friend 2025"                         # PLAY: code workflow, 0 model calls
python -m concierge.graph "play The Friend 2025 on prime"                # a named service is used (or it says where the title is)
python -m concierge.graph "play Game Changer"                            # several EXACT matches (2021, 2025) -> it asks which
python -m concierge.graph "can you play lupin?"                          # polite is fine (0 calls); one exact title among partial
                                                                         #   matches -> plays it and says what else was found
python -m concierge.graph "play tum hi ho by arijit singh"               # SONG: YouTube, 0 model calls
python -m concierge.graph "play kesariya on youtube"                     # songs need "youtube"/"yt"/"song" or "by <artist>"
python -m concierge.graph "is the tv on?"                                # STATUS: 0 model calls
python -m concierge.graph "I don't like horror, and I love Nolan films"  # TASTE: saved for the logged-in person
python -m concierge.graph "what do you know about my taste?"             # TASTE: read your own likes/dislikes
python -m concierge.graph "me and Mishika want to watch something"       # SUGGEST: both tastes (with consent)
python -m concierge.graph "I like comedy, can you suggest a few titles"  # TASTE then SUGGEST (two nodes, in order)
python -m concierge.graph "pause the tv"                                 # anything else: FALLBACK to the team


# -----------------------------------------------------------------------------------------------------
# 4. IF A REQUEST WAS INTERRUPTED (crash, closed window, PC asleep)
# -----------------------------------------------------------------------------------------------------
python -m concierge.graph --pending                       # unfinished requests, and the node each one stopped before
python -m concierge.graph --resume 20261023-171200-ab12cd # finish it from its last checkpoint (a play is never sent twice)
python -m concierge.graph --draw                          # the graph's shape as Mermaid text (paste into mermaid.live)


# -----------------------------------------------------------------------------------------------------
# 5. HOUSEHOLD SHARING (once per person; off by default; only likes/dislikes, never history)
# -----------------------------------------------------------------------------------------------------
python -m concierge.graph "share my taste with the household"            # run as EACH person (log in as each)
python -m concierge.graph "stop sharing my taste"                        # turn it off again
# Audit: who read whose taste
docker exec household-db psql -U postgres -d household -c "select * from household_reads order by id desc limit 10"


# -----------------------------------------------------------------------------------------------------
# 6. SETTINGS THAT CHANGE BEHAVIOUR (.env; restart the server windows after changing them)
# -----------------------------------------------------------------------------------------------------
# DEVICE_DRY_RUN=1          TV is never touched: play_title only says what it would do (safe default)
# DEVICE_DRY_RUN=0          LIVE: really controls the TV
# TRACING=print,jaeger      print the span tree after each run, and send spans to Jaeger
# ROUTER_MODEL=<model>      optional small model for requests the router's rules can't place
# TV_WAKE_WINDOW=25         seconds to wait for the TV on the network after Wake-on-LAN (raise if standby is slow)
# PRIME_PRESS_PLAY=1        also press Play on Prime title pages (off by default)
# CATALOG_CACHE_TTL=86400   reuse catalog answers for this many seconds


# -----------------------------------------------------------------------------------------------------
# 7. OTHER ENTRY POINTS (kept for comparison and evals; concierge/graph.py is the one to use)
# -----------------------------------------------------------------------------------------------------
python -m baselines.routed "play The Friend 2025"         # same routes, hand-written dispatch (Phase 3), not durable
python -m concierge.team "play The Friend 2025"    # the team only: Supervisor + specialists (4-6 model calls)
python -m baselines.single_agent "play The Friend 2025"          # one agent with every tool (Phases 1-2)


# -----------------------------------------------------------------------------------------------------
# 8. LOOK INSIDE (no request, or no model)
# -----------------------------------------------------------------------------------------------------
python -m baselines.single_agent --tools                          # every tool and server; full mode shows "(http, as <person>)"
python -m concierge.team --tools                    # each agent's own toolbox (no overlap)
python -m tools.groq_models                         # model ids your Groq key can use
python -m tools.mcp_probe "the friend" 2025            # call catalog search_title directly, no model
python -m tools.mcp_probe device tv_status             # read-only TV check, no model
Get-Content logs\device_mcp.log -Tail 30         # every TV action, with who asked (and idempotent repeats)
ipconfig | Select-String "IPv4"                  # TV unreachable? the PC must be on the TV's network (see README)
ping 192.168.1.6                                 # ...and must reach TV_IP; then: python -m tools.cli info


# -----------------------------------------------------------------------------------------------------
# 9. TESTS, EVALS, DRILLS (close the 3 server windows first: the tests start their own copies)
# -----------------------------------------------------------------------------------------------------
python -m pytest tests                                              # 153 tests (Keycloak + Postgres up)
python evals\drills.py                                              # 5 failure drills, ~1 min, no Groq/TV/RapidAPI
python evals\run_evals.py --agent graph --reps 3 --label graph      # evals: fake TV + catalog, real Groq
python evals\run_evals.py --compare evals\results\<A>.json evals\results\<B>.json
python evals\selftest.py                                            # proves the eval checks themselves


# -----------------------------------------------------------------------------------------------------
# 10. TV SETUP AND TUNING ONLY (talks to the TV directly: no login, no dry run)
# -----------------------------------------------------------------------------------------------------
python -m tools.cli pair                               # once: accept the prompt on the TV, saves .webos_key
python -m tools.cli wake                               # Wake-on-LAN test
python -m tools.cli apps                               # app ids installed on the TV
python -m tools.profile_probe                          # step through a Netflix profile pick (stop device_mcp first)
python -m tools.cli                                    # full list


# -----------------------------------------------------------------------------------------------------
# 11. QUICK MODE (no Docker, no login): comment out the three *_MCP_URL lines in .env, then
# -----------------------------------------------------------------------------------------------------
python -m concierge.graph "play The Friend 2025"           # catalog + device start by themselves; no taste; highlighted profile


# -----------------------------------------------------------------------------------------------------
# 12. STOP / RESET
# -----------------------------------------------------------------------------------------------------
# Close the 3 server windows (or Ctrl+C in each), then:
docker stop keycloak household-db jaeger         # your data stays; docker start brings it back
Remove-Item -Recurse .state                      # optional: forget unfinished requests, cache, breaker, idempotency records