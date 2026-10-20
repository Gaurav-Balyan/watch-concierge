# Handoff: Phase 2 done (identity and data), start of Phase 3

Attach this file, `PLAN.md`, and the project zip to a new chat. Say: "Continue the Household Watch Concierge
from HANDOFF.md. Start Phase 3."

## Who and why (unchanged)
- Gaurav, India, Windows + PowerShell, conda env `watch-concierge`. LG webOS TV on the home LAN.
- **Primary goal: learn agentic concepts.** Household use is secondary. Park business details in PLAN.md.
- Wants step-by-step guidance and small, exact changes. Send files **individually, not zipped**, with a table
  of where each goes. His files use CRLF line endings. Ask before overwriting a file he may have edited
  (he sends his latest copy when it differs, e.g. `agent.py` has a UTF-8 stdout fix in `main()`).
- LLM: Groq (`GROQ_MODEL` in .env, baseline `openai/gpt-oss-120b`). No Anthropic key.
- Versions on his machine: mcp 2.3.0, Keycloak 26.0 (Docker), Postgres 16 (Docker), PyJWT, psycopg 3.
- Teaching style that works: build a thin piece, run a visible trace, name the one concept, then a test that
  must fail in the RIGHT way. Mutation-check tests (break the code on purpose; a test must fail).
- Test before delivering: the assistant ran Keycloak 26.0.8 + Postgres 16 + mcp 2.3.0 in its own sandbox.

## What runs (Phase 2)
| Piece | How to start | Notes |
|---|---|---|
| Keycloak | `docker run -d --name keycloak -p 8080:8080 -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin -v "${PWD}\keycloak:/opt/keycloak/data/import" quay.io/keycloak/keycloak:26.0 start-dev --import-realm` | dev mode; data lives in the container; realm re-imported from `keycloak/household-realm.json` |
| Postgres | `docker run -d --name household-db -p 5433:5432 -e POSTGRES_PASSWORD=postgres-dev-pass -e POSTGRES_DB=household -v "${PWD}\db:/docker-entrypoint-initdb.d" -v household-db-data:/var/lib/postgresql/data postgres:16` | init scripts run ONCE on an empty volume; to redo: `docker rm -f household-db; docker volume rm household-db-data` |
| Login | `python login.py` / `--status` / `--logout` | PKCE in the browser; users gaurav, mishika (`<name>-dev-pass`); refresh token in `.login.json` |
| catalog-mcp | `python catalog_mcp.py --http` (8001) | or stdio (default) |
| device-mcp | `python device_mcp.py --http` (8002) | or stdio; DEVICE_DRY_RUN as before |
| taste-mcp | `python taste_mcp.py --http` (8003) | HTTP only |
| Agent | set `CATALOG_MCP_URL`, `DEVICE_MCP_URL`, `TASTE_MCP_URL` (each optional) then `python agent.py "..."` | `--tools` shows `(http, as <user>)` per tool |

Tests (Keycloak + Postgres running): `python tests\test_auth.py` (17), `test_catalog_http` (6), `test_login` (9),
`test_device_http` (6), `test_taste_http` (13), `test_profiles` (6, offline). 57 total. Evals unchanged
(`evals/run_evals.py`, stdio, no login).

## Key design facts
- One client scope per server (`catalog-mcp`, `device-mcp`, `taste-mcp`) puts that audience in the token.
  The agent logs in once, then narrows each access token to ONE server (refresh grant with `scope`).
- `auth.py`: TokenVerifier (RS256 only, iss, aud, exp, sub; fixed AuthError codes), `McpTokenVerifier` for the
  MCP SDK, `caller_name()` inside tools.
- Identity is never a tool argument. device-mcp logs "requested by <user>" and picks that user's TV profile:
  both pickers are a COLUMN that highlights the last-used profile -> UP x5, DOWN x (pos-1), ENTER. Netflix is
  closed first (close_app sometimes 403s) and given 25 s for the picker. `profile_probe.py` = step-by-step check.
- Mishika's Netflix profile is a Kids profile (Netflix blocks mature titles there).
  taste-mcp: WHERE user_id = caller AND Postgres row-level security (role `taste_app`, not superuser).
- `record_watch` is agent-only: hidden from the model, called by agent code after a real (not dry-run) play.
- Password grant is OFF (OAuth 2.1). Tests drive the real login page with `tests/kc_browser.py`.

## Open items (details in PLAN.md "Parked from Phase 2")
- Eval work: case 10 update, case 11 over-hedging, case 12 (service named in request), 12b (two preferences ->
  two remember_taste calls; seen live: one call, answer claimed both).
- `requested` is not `watched` (history gets blocked titles); kids/maturity awareness; mixed stdio/HTTP splits
  identity. Access tokens last 5 min, fetched at agent start (Phase 4: refresh + retry on 401).

## Phase 3 starting point (multi-agent orchestration)
Split the single agent into: Supervisor (routes), Taste agent (taste-mcp only), Catalog agent (catalog-mcp
only), Consensus agent ("two of us, 90 minutes, one dislikes horror"), and keep device actions with one agent.
Phase 2 already gives each specialist least privilege: give each agent only its server's token, and the
network refuses the rest (already tested). Add OpenTelemetry tracing so "who did what" is visible.
Suggested first step: a Catalog agent and a Supervisor in the same hand-written loop style, with a test that
the Catalog agent's toolbox has no device tools AND its token is refused at device-mcp.
Consensus needs several people at once: decide how a second person is identified (e.g. both log in, or the
logged-in person names the others and taste-mcp gets an explicit, audited "household read" scope). Park the
business details; the security question is the concept.
