# How a request flows (Phase 4 end state, plus Phase 5 and ideas 1 and 2a: see the end)

Two requests traced end to end through `concierge/graph.py`, in full mode (servers over HTTP, logged in as gaurav,
`DEVICE_DRY_RUN=0`):

1. `play The Friend 2025`: a code path with **0 model calls**
2. `I like comedy, can you suggest a few titles`: two agents in a row, **~5 model calls**

The same paths can be stepped through in the architecture diagram (`Watch Concierge Flow.html`, tab **NOW**).

---

## Where model calls happen (and where they never do)

The three MCP servers are plain code and never call a model: catalog-mcp (RapidAPI, yt-dlp), device-mcp (the
TV), taste-mcp (Postgres). They only answer tool calls. Every model call happens in the agent process (the
window running `concierge/graph.py`), at exactly two places in the code:

| Code | Model | When |
|---|---|---|
| `concierge/router.py`, `by_model()` | `ROUTER_MODEL` (small), 1 short call | only if the rules cannot place the request AND `ROUTER_MODEL` is set |
| `concierge/engine.py`, `Agent.loop()` | `GROQ_MODEL` | each turn of an agent: Taste, Consensus, Supervisor, Catalog |

Per graph node:

| Node | Model calls | Who decides |
|---|---|---|
| `route` | 0 (1 if the router model is used) | rules in `concierge/router.py` |
| `play`, `song`, `status` | **0** | code: `concierge/workflows.py` |
| `taste` | ~2 | the Taste agent (`concierge/skills/taste.md`) |
| `suggest` | ~3 | the Consensus agent (`concierge/skills/suggest.md`); titles confirmed by code (`check_titles`) |
| `fallback` | 1-5+ | the team: Supervisor, plus specialists through bridges |
| `ask`, `choose`, `remember` | **0** | code: pause on a question, match the reply, write the history (idea 1) |

In a trace, a model call is a `chat openai/gpt-oss-120b` span. It only ever appears under an `agent ...`
span (or the router), never inside a server.

---

## Flow 1: `python -m concierge.graph "play The Friend 2025"` (0 model calls)

```
your window (concierge/graph.py)          catalog-mcp :8001        device-mcp :8002              outside
──────────────────────          ─────────────────        ────────────────              ───────
1 main()  → request id
2 run_durable → checkpointer
3 node route (router rules)
4 checkpoint ───────────────────────────────────────────────────────────────────────► .state\checkpoints
5 node play → workflows.play_title
6   McpToolbox: 2 tokens ──────────────────────────────────────────────────────────► Keycloak
7   search_title ─────────────► cache → breaker → retry ───────────────────────────► RapidAPI (if not cached)
8   decide (code)
9   play_title + key ─────────────────────────────────► claim key ────────────────► .state\device_actions
                                                         connect / wake / open ─────► LG TV
                                                         record result
10  recorder ──────────────────────────────────────────────────────────────────────► taste-mcp → Postgres
11 checkpoint, answer, trace ──────────────────────────────────────────────────────► Jaeger
```

1. **Start.** `main()` makes a request id (e.g. `20261023-231200-ab12cd`) and prints it first, so a dead run
   can be finished with `--resume`.
2. **Durable run.** `run_durable` opens `.state\checkpoints.sqlite`. `run_graph` sets `agent.REQUEST_ID` to the
   request id (the source of the play's idempotency key) and opens the `graph` span.
3. **Node `route`.** A rule matches "play + title" and parses `title="The Friend"`, `year="2025"`. The year is
   kept only because it was typed. No model. The node writes `route = {kind: PLAY, params: {...}}` to the state.
4. **Checkpoint 1.** The state is saved before the next node runs (`durability="sync"`), with next = `play`.
   `after_route()` reads `kind == "PLAY"` and names the node.
5. **Node `play`** runs `workflows.play_title`. If a server is dead at connect, LangGraph retries the node (up
   to 3x).
6. **Narrow tokens.** `McpToolbox` gets one Keycloak token for audience `catalog-mcp` and one for `device-mcp`.
   Each server refuses any other audience. Tokens renew themselves before expiry and once after a 401.
7. **`search_title`.** Inside catalog-mcp, `lookup.search_shows`:
   - the cache, if the title was asked within the last 24 h
   - otherwise the circuit breaker, then retries with backoff, then RapidAPI, then the answer is cached
   - the year filter runs afterwards, in code

   Result: 1 match, Netflix `82182295`.
8. **Decide, in code.** One match, one service, no service named, so Netflix. The id goes from the search
   result to the play call without passing through any model.
9. **`play_title`.** Inside device-mcp:
   - `McpToolbox` adds `idempotency_key` (hidden from the model)
   - the id format is validated
   - the person comes from the token (gaurav, so Netflix profile 3)
   - BUSY check
   - the key is claimed as "started" before the TV is touched
   - `tv.reach()` (wake, then a real webOS connect within 25 s), then the Netflix flow (picker, profile, deep
     link, Play). Each wait is a `tv wait ...` span
   - the result is stored as "done" and returned as `requested`, never "playing"
10. **Recorder.** Code, not a model. After a real play, `record_watch` writes to the logged-in person's history,
    with its own `taste-mcp` token. Skipped for a dry run or a duplicate.
11. **Finish.** The answer is a template ("I sent the commands... I can't confirm playback"). `after_workflow()`
    goes to END. Checkpoint 2 marks the request done. The span tree is printed and sent to Jaeger, as one trace
    across the agent and the servers.

If it breaks:
- **Crash after step 4:** `--resume` starts at `play`.
- **Crash after the TV got the command:** `--resume` runs `play` again with the same key. device-mcp returns
  the first result, so no second launch and no second history row.
- **RapidAPI down:** a cached answer up to 7 days old, or "couldn't look that up right now" (never "not found").
- **TV unreachable:** "Nothing was launched", and the key is released so a later retry may play.

---

## Flow 2: `python -m concierge.graph "I like comedy, can you suggest a few titles"` (~5 model calls)

A preference and a request in one sentence, so the graph runs **two agents in order**: `taste`, then `suggest`.
Both agents are built from skill files: `concierge/skills/taste.md` and `concierge/skills/suggest.md` (instructions + servers + tools
+ permissions + turn budget).

```
your window (concierge/graph.py)                                     servers                    outside
──────────────────────                                     ───────                    ───────
1 node route: SUGGEST, also_taste (rules, 0 calls)
2 checkpoint (next: taste)
3 node taste: Taste agent
    model call 1 ─────────────────────────────────────────────────────────────────► Groq
      → "call remember_taste(like, comedy)"
    remember_taste ──────────────────────────────► taste-mcp ──────────────────────► Postgres (gaurav's row)
    model call 2 → answer "Saved: you like comedy." ──────────────────────────────► Groq
4 checkpoint (next: suggest)          ← a crash after this never re-saves the taste
5 node suggest: Consensus agent
    model call 3 → "call get_household_taste([gaurav])" ──────────────────────────► Groq
    get_household_taste ─────────────────────────► taste-mcp (scope + consent + audit) ► Postgres
    model call 4 → "call check_titles([4 titles])" ───────────────────────────────► Groq
    check_titles (CODE) → 4 x search_title in parallel ► catalog-mcp ─────────────► cache / RapidAPI
    model call 5 → the answer (confirmed titles only) ────────────────────────────► Groq
6 checkpoint (done), answer, trace
```

1. **Node `route`.** Two rules match: a preference ("I like ...") and a request for suggestions ("suggest").
   Route `SUGGEST` with `also_taste: true`. Still **no model**.
2. **Edge `after_route()`** sees `also_taste`, so it goes to `taste` first (save the preference before
   suggesting). This needs the taste server, i.e. a login; without one, the request goes to the team instead.
3. **Node `taste`: the Taste agent.** This is the `Agent.loop` in `concierge/engine.py`. The agent's toolbox has only
   taste-mcp, with a token for audience `taste-mcp`. It is shown `get_my_taste`, `remember_taste`,
   `forget_taste` and `set_household_sharing`. `get_household_taste` is hidden from it, and `record_watch` is
   for code only.
   - **Model call 1.** The model gets the skill's instructions plus the request and the offered tools. It
     answers with a tool call: `remember_taste(kind="like", item="comedy")`. The model **proposes**; code
     checks the tool is offered, then runs it.
   - **taste-mcp** saves the row for the person in the token (gaurav), whatever the words say. It runs inside
     gaurav's row-level-security transaction in Postgres.
   - **Model call 2.** The tool result goes back to the model, which writes "Saved: you like comedy."
4. **Checkpoint 2:** taste is done, next = `suggest`. This is the point of making it two nodes: a crash during
   `suggest` resumes at `suggest`, and the preference is not saved twice.
5. **Edge `after_taste()`.** The route is SUGGEST, so `suggest` runs next. The node tells the agent who is
   watching: "Watching: gaurav ...". That name comes from the login state, by code.
6. **Node `suggest`: the Consensus agent.** It is shown only `get_household_taste` and `check_titles`. Its
   taste token **also** carries the extra permission `taste-household-read`; it is the only agent that asks
   for it.
   - **Model call 3** → `get_household_taste(names=["gaurav"])`. taste-mcp checks the permission, membership
     and each named person's own consent, reads each person in their own transaction (likes and dislikes only,
     never history), and writes every lookup to the `household_reads` audit table.
   - **Model call 4.** With "likes comedy" (and any dislikes as a hard no), the model proposes up to 4 titles
     and calls `check_titles` once.
   - **`check_titles` is code, not a model.** It opens its own catalog-mcp connection (token `catalog-mcp`
     only) and runs 4 `search_title` calls **in parallel**, each through the cache, breaker and retries. It
     returns only the titles the catalog confirms, with year, service and content id.
   - **Model call 5.** The answer lists only the confirmed titles, plus who was included and why.
7. **Finish.** `suggest`'s answer replaces `taste`'s in the state (LangGraph merges updates in order). Then
   END, checkpoint 3, the printed tree and Jaeger. **Nothing is played**: you pick a title, then make a new
   request, e.g. `python -m concierge.graph "play <title> <year>"`.

What the trace looks like (shape; exact numbers vary):

```
graph
├─ node route
├─ node taste
│  └─ agent taste
│     ├─ connect taste             as gaurav  aud=taste-mcp
│     ├─ chat openai/gpt-oss-120b  tools=[get_my_taste, remember_taste, ...]   -> tool_calls
│     ├─ execute_tool remember_taste  {"kind": "like", "item": "comedy"}
│     └─ chat openai/gpt-oss-120b                                               -> stop
└─ node suggest
   └─ agent consensus
      ├─ connect taste             as gaurav  aud=taste-mcp +taste-household-read
      ├─ chat ...                  tools=[get_household_taste, check_titles]   -> tool_calls
      ├─ execute_tool get_household_taste  {"names": ["gaurav"]}
      ├─ chat ...                                                               -> tool_calls
      ├─ execute_tool check_titles
      │  ├─ connect catalog        aud=catalog-mcp
      │  └─ execute_tool search_title  x4 (parallel)
      └─ chat ...                                                               -> stop
```

---

## The two flows side by side

| | Flow 1: play | Flow 2: like + suggest |
|---|---|---|
| Route | PLAY (rule) | SUGGEST + also_taste (rule) |
| Nodes | route → play | route → taste → suggest |
| Who decides the steps | code (`concierge/workflows.py`) | models, inside the limits code sets |
| Model calls | **0** | ~5 (2 Taste + 3 Consensus) |
| Servers and tokens | catalog-mcp, device-mcp (+ taste-mcp for the recorder) | taste-mcp (Consensus: + `taste-household-read`), catalog-mcp via `check_titles` |
| What code guarantees | year only if typed; id from the search; never claims playback; plays once | identity from the token; household read needs scope + consent; only confirmed titles; taste saved once |
| Acts on the TV | yes (idempotent) | no |
| Retried by the graph | yes, if a server died | no (a repeat would redo every model call; Groq retries live in the SDK) |

The rule behind both: **code decides whatever must always hold** (who is asking, which ids are real, what may
run twice). **A model decides only what needs judgement** (which preference was meant, which titles might fit),
and its proposals are checked by code before they count.

---

## Added after Phase 4 (Phase 5, ideas 1 and 2a)

### Flow 3: a question and its answer (`--chat`, 0 model calls)
```
you> play game changer
  route (PLAY) -> play: search_title finds 2 exact titles -> an Ask: the question + 2 OPTIONS (service, id
  from the search) -> ask: interrupt()  ==> the run STOPS; the checkpoint keeps the question and the options
you> the 2021 one
  Command(resume="the 2021 one") on the same thread -> ask runs again from its first line, interrupt() returns
  the reply -> choose: pick_option (code) matches "2021" -> play_title with the SAVED id -> remember -> END
```
The reply can only SELECT a saved option: it cannot bring an id. A line that is a new request instead
("actually, is the tv on?") closes the question first (`turn_kind`, the routing rules decide).

### Flow 4: "play the first one" after a suggestion (message memory, ~2 model calls)
```
you> suggest a comedy for me     -> suggest / fallback answers with confirmed titles -> remember writes
                                    [user: request, assistant: answer] to the thread's history
you> play the first one          -> route: a PLAY of only reference words + history -> FALLBACK
                                 -> the Supervisor gets system + history + this request (no tool results)
                                 -> play_title: allowed because the id is in known_ids (code-saved from the
                                    earlier SEARCH), not because the history text mentions it
```
One conversation = one thread; each request is a turn with its own request id (`<thread>#<turn>`), so two plays in
one chat are two plays. The last 3 turns are kept.

### Flow 5: the Catalog agent over A2A (optional, `CATALOG_AGENT_URL` set)
```
Supervisor --ask_catalog_agent--> concierge/remote.py
   GET  /.well-known/agent-card.json             (discovery: who it is, where to send messages)
   POST /  SendMessage "Where is ... streaming?" (JSON-RPC)  ==> catalog-agent process (:8004)
                                                     Catalog agent (its own model) --MCP--> catalog-mcp
   <== Task COMPLETED, artifacts: "answer" (model TEXT) + "searched_ids" (DATA, written by CODE)
Supervisor's process registers ONLY the searched_ids artifact -> play_title is allowed for those ids
```
Model calls then also happen in the catalog-agent process (its own `agent catalog` spans, in its own trace).

### What Phase 5 added to every agent path
- **Provenance guard** (`engine.SEARCHED_IDS`): a model's `play_title` needs a (service, id) that a real search returned
  in this request (or, in a conversation, an earlier turn): an id written in a title, an answer or the history is
  BLOCKED. Text in search results is data, never instructions.
- **The Taste agent hears only the person**: on the team path, code passes your own words to it, never the
  Supervisor's.
