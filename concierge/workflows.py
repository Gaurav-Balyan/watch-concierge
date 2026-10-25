"""
Phase 3, Step 7: WORKFLOWS. Fixed steps in code for requests that need no judgement.

    PLAY   search_title -> decide (code) -> play_title -> a fixed sentence
    SONG   search_youtube -> play the first video -> a fixed sentence
    STATUS tv_status -> a fixed sentence

0 model calls. The POLICIES that were prompt lines (and that the evals showed holding only sometimes)
are `if` statements here, so they always hold and a unit test can prove them:
    * a year only if the person typed one (never guessed)
    * a title on BOTH services and no service named -> ask which; a service named -> use it
    * several matching titles -> ask which; none -> say so (never blame the title for a system error)
    * the content id goes from search_title to play_title by CODE (no model copies it)
    * the answer never claims playback: the TV cannot confirm it
Each workflow opens only the servers it needs (one narrow token each). The final sentence is a
TEMPLATE, not model text: it costs nothing and cannot overclaim; the price is plainer wording.

Idea 1 (multi-turn): when PLAY has to ask ("Netflix or Prime?", "which Game Changer?"), it returns an
Ask: the question text PLUS the options code will accept, each a (service, content id) from the search.
The graph pauses on it and later plays the option the reply picks (pick_option, code, no model).
"""
import json
import re

from concierge import engine
from concierge import recorder

SERVICE_NAMES = {"netflix": "Netflix", "prime": "Prime Video", "youtube": "YouTube"}


def _servers(*names) -> dict:
    return {n: engine.SERVERS[n] for n in names}


def same_title(a: str, b: str) -> bool:
    """'LUPIN' == 'Lupin' == 'lupin!': case, punctuation and spacing ignored. Nothing fuzzier: a title
    the person did not ask for is never played on a guess."""
    def norm(t):
        return " ".join(re.sub(r"[^\w]+", " ", str(t).casefold()).split())
    return norm(a) == norm(b)


def _label(m: dict) -> str:
    return f"{m['title']} ({m['year']})" if m.get("year") else m["title"]


class Ask(str):
    """A QUESTION to the person. It IS the question text (a str), so everything that shows answers keeps
    working; `.options` are the only things a reply can choose: [{label, service, content_id, title}],
    straight from the search result (plain data, so the graph can save it in a checkpoint)."""
    options: list

    def __new__(cls, text: str, options: list):
        obj = super().__new__(cls, text)
        obj.options = options
        return obj


def _options(matches: list[dict]) -> list[dict]:
    """One option per (title, service): what a reply can pick."""
    return [{"label": f"{_label(m)} on {SERVICE_NAMES[s]}", "service": s, "content_id": m["ids"][s],
             "title": _label(m)} for m in matches for s in m["ids"]]


_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3}
_SERVICE_WORDS = {"netflix": "netflix", "prime": "prime", "amazon": "prime", "youtube": "youtube"}
_YES = re.compile(r"^\s*(yes|yeah|yep|yup|ok|okay|sure|go ahead|do it|play it|please)\b", re.I)
_CANCEL = re.compile(r"^\s*(no|nope|cancel|stop|never\s*mind|nothing|leave it|forget it)\b", re.I)


def pick_option(reply: str, options: list[dict]):
    """Which option a reply means, by CODE: an option dict, "cancel", or None (unclear).
    Reads a number or ordinal ("2", "the second"), a service ("prime"), a year ("2021"), or "yes" when
    there is only one option. Anything else is unclear: we ask again rather than guess."""
    text = " ".join(str(reply).casefold().split())
    words = re.findall(r"[\w]+", text)
    found, used = list(range(len(options))), False
    numbers = [int(w) - 1 for w in words if w.isdigit() and len(w) == 1] + [_ORDINALS[w] for w in words if w in _ORDINALS]
    if numbers:
        found, used = [i for i in found if i in numbers], True
    services = {_SERVICE_WORDS[w] for w in words if w in _SERVICE_WORDS}
    if services:
        found, used = [i for i in found if options[i]["service"] in services], True
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", text))
    if years:
        found, used = [i for i in found if any(y in options[i]["title"] for y in years)], True
    if used and len(found) == 1:
        return options[found[0]]
    if not used and len(options) == 1 and _YES.match(text):
        return options[0]
    if _CANCEL.match(text):
        return "cancel"
    return None


async def _play(box, service: str, content_id: str, label: str, trace, who: str, note: str = "") -> str:
    """`note` is added to a SUCCESSFUL answer only (e.g. which other titles the search also found)."""
    args = {"service": service, "content_id": content_id, "title": label}
    text, is_error = await engine.call_traced(box, "play_title", args, trace, who)
    if is_error:
        if "TV_UNREACHABLE" in text:
            return ("The TV didn't respond: it may be off or not on the network. Nothing was launched. "
                    "Turn it on and ask again.")
        if "BUSY" in text:
            return "Another TV action is still running. Nothing new was started; ask again in a moment."
        return f"I couldn't open {label}: {' '.join(text.split())[:200]}"
    result = json.loads(text)
    name = SERVICE_NAMES.get(service, service)
    if result.get("status") == "dry_run":
        return (f"That was only a simulation (dry run): no command was sent to the TV. "
                f"It would have opened {label} on {name}.{note}")
    if result.get("duplicate"):
        # Phase 4: a resumed / retried run of THIS request. device-mcp recognised its idempotency key.
        # The person may never have seen the first answer (a crash), so say what was done.
        return (f"I sent the commands to open {label} on {name}. (This request was interrupted and "
                "resumed; the commands had already gone out, so they were not sent a second time.) "
                f"I can't confirm playback: the TV doesn't report it.{note}")
    await recorder.record_watch_after_play(None, "play_title", args, text)      # code, never a model
    if result.get("next_step_for_user"):
        return f"I sent the commands to open {label} on {name}. {result['next_step_for_user']}{note}"
    return (f"I sent the commands to open {label} on {name} and press Play. "
            f"I can't confirm playback: the TV doesn't report it.{note}")


async def play_title(params: dict, trace: list | None) -> str | None:
    """None = 'not mine' (the router should hand the request to the team)."""
    title, year, wanted = params.get("title"), params.get("year"), params.get("service")
    if not title:
        return None
    who = "workflow:play"
    async with engine.McpToolbox(_servers("catalog", "device")) as box:
        args = {"title": title, **({"year": year} if year else {})}          # a year ONLY if typed
        text, is_error = await engine.call_traced(box, "search_title", args, trace, who)
        if is_error:
            # A SYSTEM problem, not the title's fault: say so, and do not say "not found".
            return ("I couldn't look that up right now: the catalog service had a problem. "
                    "Please try again later.")
        matches = json.loads(text).get("matches", [])
        if not matches:
            return f"I couldn't find \"{title}\"{f' ({year})' if year else ''} on Netflix or Prime in India."
        note = ""
        exact = [m for m in matches if same_title(m["title"], title)]
        if len(matches) > 1 and len(exact) == 1:
            # The policy (2026-10-24): exactly ONE result has the title the person asked for ("Lupin"), the
            # others only contain the words ("LUPIN THE 3rd vs. CAT'S EYE"): play that one, and say what else
            # was found. Several EXACT matches (Game Changer 2021 and 2025) are still a real question.
            others = "; ".join(f"{_label(o)} on {' and '.join(SERVICE_NAMES[s] for s in o['ids'])}"
                               for o in matches if o is not exact[0])
            note = f" (The search also found: {others}. Say so if you meant one of those.)"
            matches = exact
        if len(matches) > 1:
            ordered = sorted(matches, key=lambda m: not same_title(m["title"], title))   # exact matches first
            options = "; ".join(f"{_label(m)} on {' and '.join(SERVICE_NAMES[s] for s in m['ids'])}"
                                for m in ordered)
            return Ask(f"I found {len(matches)} titles called {title}: {options}. Which one do you mean?",
                       _options(ordered))
        m = matches[0]
        services = list(m["ids"])
        if wanted:
            if wanted not in m["ids"]:
                where = " and ".join(SERVICE_NAMES[s] for s in services)
                return Ask(f"{_label(m)} is not on {SERVICE_NAMES[wanted]}, it is on {where}. Shall I play it there?",
                           _options([m]))
            service = wanted
        elif len(services) > 1:
            return Ask(f"{_label(m)} is on both Netflix and Prime Video. Which one would you like?",   # the decided policy
                       _options([m]))
        else:
            service = services[0]
        return await _play(box, service, m["ids"][service], _label(m), trace, who, note)


async def play_option(option: dict, trace: list | None) -> str:
    """Play what the person picked from an Ask's options (idea 1). The id comes from the SAVED search
    result (plain data written by code into the checkpoint), never from the reply's words."""
    async with engine.McpToolbox(_servers("device")) as box:
        return await _play(box, option["service"], option["content_id"], option["title"], trace, "workflow:choose")


async def play_song(params: dict, trace: list | None) -> str | None:
    query = params.get("query")
    if not query:
        return None
    who = "workflow:song"
    async with engine.McpToolbox(_servers("catalog", "device")) as box:
        text, is_error = await engine.call_traced(box, "search_youtube", {"query": query}, trace, who)
        if is_error:
            return "I couldn't search YouTube right now: the search had a problem. Please try again later."
        videos = json.loads(text).get("videos", [])
        if not videos:
            return f"I couldn't find \"{query}\" on YouTube."
        v = videos[0]
        label = v["title"] + (f" ({v['channel']})" if v.get("channel") else "")
        return await _play(box, "youtube", v["video_id"], label, trace, who)


async def tv_status(params: dict, trace: list | None) -> str:
    async with engine.McpToolbox(_servers("device")) as box:
        text, is_error = await engine.call_traced(box, "tv_status", {}, trace, "workflow:status")
    if is_error:
        return f"I couldn't check the TV: {' '.join(text.split())[:200]}"
    return status_answer(json.loads(text))


def status_answer(s: dict) -> str:
    """The sentence for a tv_status result (a template: it says only what the result says)."""
    if not s.get("reachable"):
        where = f" at {s['tv_ip']}" if s.get("tv_ip") else ""
        if s.get("network_answers") is False:
            why = (f"nothing answers{where}. Either the TV is off (or in deep standby), or this PC is not on the "
                   "TV's network (see README: 'TV not reachable?').")
        elif s.get("network_answers"):
            why = f"it answers{where} but refused the remote-control connection ({s.get('error')})."
        else:
            why = "it may be off or in deep standby."
        return f"The TV isn't answering: {why} Playing a title wakes it from standby." + (
            f" (Error: {s['error']})" if s.get("error") and s.get("network_answers") is not True else "")
    app = s.get("foreground_app")
    return f"Yes, the TV is on (power state: {s.get('power_state')})" + (f", showing {app}." if app else ".")