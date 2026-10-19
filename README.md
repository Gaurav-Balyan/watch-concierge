# Household Watch Concierge

Say a title, and it opens on the LG TV: it looks the title up on Netflix and Prime Video (India), wakes the TV, and drives the right app. Songs play on YouTube.

## Setup

```powershell
pip install -r requirements.txt
copy .env.example .env      # then fill in TV_IP, TV_MAC, RAPIDAPI_KEY
python cli.py pair          # accept the prompt on the TV (once)
```

`.webos_key` (the pairing key) is saved in this folder. If you are moving from the old `phase0_spike.py` folder, copy its `.webos_key` here and you won't need to pair again.

## Commands

```powershell
python cli.py watch "The Friend"          # Netflix or Prime, whichever has it
python cli.py watch "The Boys" 2019       # optional year
python cli.py watch "Game Changer" yes    # force pressing Play
python cli.py watchnetflix "The Friend"   # one service only
python cli.py watchprime "The Boys"
python cli.py song tum hi ho arijit singh
python cli.py                             # full command list
```

## Layout

| File | Job |
|---|---|
| `config.py` | Reads `.env`; every delay and setting lives here |
| `tv.py` | Connect, wake, press keys, send links. Knows nothing about services |
| `lookup.py` | Title name to per-service IDs. No TV code, testable offline |
| `services/netflix.py` | Netflix flow: profile first, then link, then Play |
| `services/prime.py` | Prime flow: link at the profile picker, then pick the profile |
| `services/youtube.py` | YouTube search (yt-dlp) and play |
| `concierge.py` | `watch()` and `song()`: the layer a chat or voice front end calls |
| `cli.py` | Command-line entry point |
| `agent.py` | Phase 1 Step 4: the agent loop (Groq); tools are discovered from the MCP servers |
| `agent_inline_tools.py` | Reference: Step 1 version with tools written inside the file |
| `catalog_mcp.py` | Phase 1 Step 2: MCP server, `search_title` (read-only) |
| `device_mcp.py` | Phase 1 Step 3: MCP server, `tv_status` and `play_title` (acts on the TV; dry run by default) |
| `logsetup.py` | Log file + print-capture for the MCP servers (`logs/device_mcp.log`) |
| `evals/` | Phase 1 Step 6: eval cases, fake catalog and TV, runner, self-test (see below) |
| `mcp_probe.py` | Test any MCP server without a model: `python mcp_probe.py device tv_status` |
| `experiments.py` | Rarely used diagnostics: `apps`, `services`, `avail`, `keys`, `probe` |

### Adding another service (JioHotstar, SonyLIV, ...)

1. Add `services/<name>.py` with `NAME`, `DEFAULT_PLAY` and `open_title(c, content_id, press_play)`.
2. Teach `lookup.extract_ids` to read its ID from the API's links.
3. Register it in `services/__init__.py` and add it to `SERVICE_PREFERENCE`.

## What was verified on the TV, and what was not

Verified during Phase 0:
- Wake-on-LAN, pairing, Netflix deep link (warm start only), YouTube deep link.
- Prime: the link works when sent while the profile picker shows; the first ENTER then picks the profile and a second ENTER presses Play.

Not verified, or fragile:
- **Fixed waits, not checks.** Every delay is a sleep; nothing confirms a screen has loaded. Raise the values in `.env` if a step fires early.
- **Prime wait.** `PRIME_LINK_TO_KEY_WAIT` defaults to 15 because ENTER at 6 seconds did not pick the profile. Lower it gradually once it is reliable.
- **`close_app`** is untested on this TV; if it fails, the script prints a note and continues.
- **Prime Play** is off by default so ENTER is never sent blindly (the focused button could be rent/buy). Use `yes` to opt in. Check a few titles, including a series, first.
- **Profiles.** Both flows press ENTER on the first profile.
- **`/24` network** is assumed when building the wake broadcast address.
- **`catch CancelledError`** in `tv.send_link` is a spike shortcut, not production code.

## Security

`.env` and `.webos_key` are in `.gitignore`. If an API key was ever pasted into a chat or committed, regenerate it.

## Evals (Phase 1, Step 6)

Fake catalog + fake TV, so nothing touches your TV or your RapidAPI quota. They do use Groq calls.

```powershell
python evals/selftest.py                      # test the tests first (no API needed)
python evals/run_evals.py --reps 3 --label baseline
python evals/run_evals.py --only 2,3 --show   # some cases, and print failing answers
python evals/run_evals.py --compare evals/results\A.json evals/results\B.json
```

Change ONE thing between two runs (a prompt line, a tool description, the model), run again with a new
`--label`, and compare. The compare output lists what differs between the runs and warns if more than one thing did.

conda activate "C:\Users\Gaurav\anaconda3\envs\watch-concierge"
