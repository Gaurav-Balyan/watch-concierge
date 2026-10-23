"""
Phase 3, Step 7: the ROUTER. Decide the path BEFORE any expensive agent work.

    request --> rules (free, instant) --> sure?  yes -> that route
                                             no  -> a SMALL model (ROUTER_MODEL, 1 cheap call), if set
                                                    unsure / not set -> FALLBACK (the full team)

ROUTES
    PLAY     "play The Friend 2025"           -> workflows.play_title   (code, 0 model calls)
    SONG     "play tum hi ho by arijit singh"  -> workflows.play_song    (code, 0 model calls)
    STATUS   "is the tv on?"                   -> workflows.tv_status    (code, 0 model calls)
    TASTE    "I don't like horror"             -> skills/taste.md        (an agent: judgement needed)
    SUGGEST  "me and Mishika, something ..."   -> skills/suggest.md      (an agent: judgement needed)
    FALLBACK anything else                     -> multi_agent.run_team   (the Phase 3 team)

A wrong route is the new failure mode (a film named like a song...). Two defences: rules only claim
what they are SURE of (otherwise "unsure"), and every workflow hands over to FALLBACK when its own
input does not fit (e.g. an empty title). The trace records the route and why.
"""
import json
import re
from dataclasses import dataclass, field

import config

ROUTES = ("PLAY", "SONG", "STATUS", "TASTE", "SUGGEST", "FALLBACK")


@dataclass(frozen=True)
class Route:
    kind: str
    reason: str
    source: str                       # "rule" | "model" | "default"
    params: dict = field(default_factory=dict)


# ---------------------------------------------------------------- rules (free)

_VAGUE = {"something", "something good", "anything", "a movie", "a show", "a film", "something nice"}
_STATUS = re.compile(r"\b(tv|television)\b.{0,20}\b(on|off|awake|asleep|status)\b|\bis (the )?tv\b", re.I)
# "you tube" / "yt" too: seen live, "play taaki taaki rumba on you tube" was routed to PLAY (a film search).
_YOUTUBE = r"\byou\s*tube\b|\byt\b"
_SONG = re.compile(_YOUTUBE + r"|\bsong\b|\bmusic\b|\bmusic video\b|\btrack\b|^\s*play\s+.+\s+by\s+\w", re.I)
_SUGGEST = re.compile(r"\b(suggest|recommend|recommendation|what should (i|we) watch|something (good|nice|to watch)|"
                      r"two of us|together|me and \w+|\w+ and me)\b", re.I)
_TASTE = re.compile(r"^\s*(i|we)\s+(really\s+)?(like|love|hate|dislike|enjoy|adore|can'?t stand|don'?t (like|enjoy))\b|"
                    r"\bforget that i\b|\b(share|stop sharing) my taste\b|\bmy taste\b", re.I)
_PLAY = re.compile(r"^\s*(play|watch|open|put on|start|stream)\s+(?P<rest>.+?)\s*[.!?]*\s*$", re.I)
_SERVICE = re.compile(r"\s+on\s+(?P<service>netflix|prime(?: video)?|amazon prime)\s*$", re.I)
_YEAR = re.compile(r"\s*\(?\b(?P<year>(19|20)\d{2})\b\)?\s*$")
_KIND_WORDS = re.compile(r"\s+(the\s+)?(movie|film|series|tv series|show|tv show)\s*$", re.I)


def parse_play(rest: str) -> dict:
    """'The Friend 2025 on prime' -> {title: 'The Friend', year: '2025', service: 'prime'}.
    A year only if the person TYPED one (Step 6: an invented year made a real title 'not available')."""
    out = {}
    m = _SERVICE.search(rest)
    if m:
        out["service"] = "netflix" if m.group("service").lower() == "netflix" else "prime"
        rest = rest[:m.start()]
    rest = _KIND_WORDS.sub("", rest)
    m = _YEAR.search(rest)
    if m and m.start() > 0:
        out["year"] = m.group("year")
        rest = rest[:m.start()]
    out["title"] = " ".join(rest.split()).strip(" '\"")
    return out


def song_query(request: str) -> str:
    q = re.sub(r"^\s*(play|put on|start)\s+", "", request, flags=re.I)
    q = re.sub(r"\s+on\s+(" + _YOUTUBE + r")\s*$|" + _YOUTUBE + r"|\bthe song\b|\bsong\b|\bmusic video\b", " ", q, flags=re.I)
    return " ".join(q.split()).strip(" .!?'\"")


def by_rules(request: str) -> Route | None:
    """A route the rules are SURE of, or None."""
    text = " ".join(request.split())
    if _STATUS.search(text) and not _PLAY.match(text):
        return Route("STATUS", "asks about the TV's state", "rule")
    if _SONG.search(text):
        q = song_query(text)
        return Route("SONG", "mentions YouTube / a song / 'play X by Y'", "rule", {"query": q}) if len(q) >= 2 else None
    if _TASTE.search(text) and not _SUGGEST.search(text):
        return Route("TASTE", "states or asks about the person's own taste", "rule")
    if _SUGGEST.search(text):
        # "I like comedy, can you suggest ..." = save the preference FIRST, then suggest.
        return Route("SUGGEST", "asks for suggestions / several people", "rule",
                     {"also_taste": bool(_TASTE.search(text))})
    m = _PLAY.match(text)
    if m:
        params = parse_play(m.group("rest"))
        if params["title"].lower() in _VAGUE or len(params["title"]) < 2:
            return None                                  # "play something good": not a title
        return Route("PLAY", "starts with play/watch + a title", "rule", params)
    return None


# ---------------------------------------------------------------- small model (1 cheap call)

ROUTER_PROMPT = (
    "Classify a request to a TV concierge in India. Answer with ONLY a JSON object "
    '{"route": "<ROUTE>", "title": "<title or song, if any>"} where ROUTE is one of: '
    "PLAY (open a specific movie or series), SONG (play a song or music video on YouTube), "
    "STATUS (is the TV on), TASTE (the person states or asks about their own likes/dislikes), "
    "SUGGEST (wants suggestions, or several people watching), FALLBACK (anything else or unsure). "
    "If unsure, answer FALLBACK."
)


async def by_model(request: str) -> Route | None:
    """Only when ROUTER_MODEL is set. A small, fast model; FALLBACK on anything odd."""
    if not config.ROUTER_MODEL:
        return None
    import agent
    from tracing import span
    from tracing import failed
    with span("chat " + config.ROUTER_MODEL, {"gen_ai.operation.name": "chat", "gen_ai.agent.name": "router",
                                              "gen_ai.request.model": config.ROUTER_MODEL}) as s:
        try:
            response = await agent.AsyncGroq().chat.completions.create(
                model=config.ROUTER_MODEL, max_tokens=60, temperature=0,
                messages=[{"role": "system", "content": ROUTER_PROMPT}, {"role": "user", "content": request}])
        except agent.APIError as e:
            # The router model is an OPTIONAL helper: if it fails (wrong name, rate limit, outage) the
            # request still works, through the team. Seen live: a model name the key could not use (404).
            failed(s, f"{type(e).__name__}: {e}")
            print(f"[router] ROUTER_MODEL {config.ROUTER_MODEL!r} failed ({type(e).__name__}); using the team")
            return Route("FALLBACK", f"router model failed: {type(e).__name__}", "model")
    text = response.choices[0].message.content or ""
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return Route("FALLBACK", f"router model gave no JSON: {text[:60]!r}", "model")
    kind = str(data.get("route", "")).upper()
    if kind not in ROUTES:
        return Route("FALLBACK", f"router model said {kind!r}", "model")
    title = " ".join(str(data.get("title") or request).split())
    # The SAME parsing as the rules (seen in the evals: "game changer 2025" from the model was searched as one
    # title, year included, and found nothing). A year/service is kept only if it is in the person's words.
    if kind == "PLAY":
        params = parse_play(title)
        if "year" in params and params["year"] not in request:
            params.pop("year")                      # the model added a year the person did not type
        # ...and the reverse (seen in the Phase 4 evals, case 4): the model returned "game changer" for
        # "game changer 2025", DROPPING the typed year, so the search found two titles and asked. A year or
        # service the person typed comes from THEIR words, whatever the model's title says.
        typed = parse_play(" ".join(request.split()))
        for key in ("year", "service"):
            if key in typed and key not in params:
                params[key] = typed[key]
    else:
        params = {"query": song_query(title)} if kind == "SONG" else {}
    return Route(kind, "classified by the router model", "model", params)


async def route(request: str) -> Route:
    return (by_rules(request) or await by_model(request)
            or Route("FALLBACK", "no rule matched and no router model set", "default"))