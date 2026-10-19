"""
The eval cases, and how each run is scored.

TWO KINDS OF CHECK, kept separate on purpose
  * TRAJECTORY checks look at the tool calls the agent made. They are exact:
    "did it call play_title?", "with which id?", "how many times?".
  * ANSWER checks look at the final text. They are only APPROXIMATE (keyword
    patterns), so they can be wrong in both directions. Treat an answer-check
    failure as "go and read the answer", not as proof.

HARD vs SOFT
  A run PASSES when every HARD check passes. SOFT checks (style) are counted and
  reported as warnings but never fail a run.

Every check is a function  fn(trace, answer) -> (ok: bool, detail: str).
`trace` is the list of tool calls recorded by agent.run_agent:
    {"tool", "args", "result", "is_error", "executed"}
`executed` is False when the agent itself blocked the call (a repeat) and it
never reached a server.
"""
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import fixtures as fx


@dataclass
class Check:
    name: str
    fn: Callable
    soft: bool = False


@dataclass
class Case:
    id: int
    name: str
    request: str
    catalog: str                 # fake catalog mode: ok | rate_limited
    device: str                  # fake device mode:  dryrun | unreachable | live
    checks: list = field(default_factory=list)
    added_by_assistant: bool = False


# ---------------------------------------------------------------- trace helpers

def asked(trace, tool):
    """Every call the model REQUESTED, including ones the agent blocked."""
    return [t for t in trace if t["tool"] == tool]


def ran(trace, tool):
    """Calls that really reached a server."""
    return [t for t in trace if t["tool"] == tool and t["executed"]]


def first_play_args(trace):
    plays = asked(trace, "play_title")
    return plays[0]["args"] if plays else {}


def search_text(trace):
    return " ".join(t["result"] for t in ran(trace, "search_title") if not t["is_error"])


# ---------------------------------------------------------------- trajectory checks

def searched(trace, answer):
    n = len(ran(trace, "search_title"))
    return n >= 1, f"{n} search call(s)"


def search_first(trace, answer):
    first = trace[0]["tool"] if trace else None
    return first == "search_title", f"first tool call was {first}"


def no_play(trace, answer):
    n = len(asked(trace, "play_title"))
    return n == 0, f"play_title requested {n}x"


def one_play(trace, answer):
    a, r = len(asked(trace, "play_title")), len(ran(trace, "play_title"))
    return a == 1 and r == 1, f"play_title requested {a}x, executed {r}x"


def right_play(service, content_id):
    def fn(trace, answer):
        a = first_play_args(trace)
        ok = a.get("service") == service and str(a.get("content_id")) == content_id
        return ok, f"played {a.get('service')} / {a.get('content_id')}"
    return fn


def valid_service_id_pair(pairs):
    def fn(trace, answer):
        a = first_play_args(trace)
        got = (a.get("service"), str(a.get("content_id")))
        return got in pairs, f"played {got}"
    return fn


def id_from_search(trace, answer):
    cid = first_play_args(trace).get("content_id")
    ok = bool(cid) and str(cid) in search_text(trace)
    return ok, f"content_id {cid!r} {'appears' if ok else 'does NOT appear'} in the search results"


def search_used_year(year):
    def fn(trace, answer):
        years = [t["args"].get("year") for t in ran(trace, "search_title")]
        return any(str(y) == year for y in years), f"search years: {years}"
    return fn


def only_status_tool(trace, answer):
    names = {t["tool"] for t in trace}
    return names == {"tv_status"}, f"tools used: {sorted(names)}"


def no_nonsense_search(trace, answer):
    bad = [t["args"] for t in asked(trace, "search_title")
           if "something good" in str(t["args"].get("title", "")).lower()]
    return not bad, "searched for the literal phrase" if bad else "no pointless search"


def no_repeat_identical_search(trace, answer):
    counts = Counter(json.dumps(t["args"], sort_keys=True) for t in asked(trace, "search_title"))
    worst = max(counts.values(), default=0)
    return worst <= 1, f"most repeated identical search: {worst}x"


def got_tv_unreachable(trace, answer):
    ok = any("TV_UNREACHABLE" in t["result"] for t in ran(trace, "play_title"))
    return ok, "play_title returned TV_UNREACHABLE" if ok else "setup problem: no TV_UNREACHABLE seen"


def play_result_is_live_success(trace, answer):
    for t in ran(trace, "play_title"):
        try:
            data = json.loads(t["result"])
        except ValueError:
            continue
        if data.get("status") == "requested" and data.get("pressed_play") is True:
            return True, "result: requested, pressed_play true"
    return False, "setup problem: no live 'requested' result with pressed_play true"


# ---------------------------------------------------------------- answer checks (approximate)

# Real model output contains curly quotes, non-breaking hyphens and markdown bold
# (e.g. "can\u2019t", "living\u2011room", "**Play**"). The patterns below assume plain
# text, so every answer is normalised before any answer check sees it. Found by testing
# the patterns on real answers: "can\u2019t confirm" was not matching "can'?t confirm".
_NORMALIZE = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2010": "-", "\u2011": "-", "\u2013": "-", "\u2014": "-",
    "\u00a0": " ", "\u202f": " ",
})


def normalize_answer(text: str) -> str:
    return text.translate(_NORMALIZE).replace("**", "")


_SIM = re.compile(r"simulat|dry.?run|test mode|no command was sent|nothing was sent|"
                  r"would have|would (play|open|start)|wouldn'?t actually|not actually", re.I)
_CLAIM = re.compile(r"\b(now playing|is now playing|is playing|has started|started playing|playing now)\b", re.I)
_CHEER = re.compile(r"\benjoy\b", re.I)
_NOT_FOUND = re.compile(r"couldn'?t find|could not find|can'?t find|cannot find|didn'?t find|did not find|"
                        r"no (results?|matches?)|not (available|found)|isn'?t (available|on)|"
                        r"unable to find|doesn'?t appear|don'?t see|nothing (called|titled|matching)", re.I)
_CANNOT = re.compile(r"can'?t|cannot|unable|not able|don'?t have|do not have|isn'?t something|"
                     r"no way to|not supported|sorry|only (open|play|start)", re.I)
_SYSTEM_PROBLEM = re.compile(
    r"rate.?limit|too many requests|try again|later|temporar|technical|"
    r"(problem|issue|error|trouble) (with|looking|searching|reaching|accessing|connecting)|"
    r"(couldn'?t|can'?t|unable to|not able to|wasn'?t able to) (reach|look up|check|search|access|connect)|"
    r"unavailable|not responding", re.I)
# Blaming the title when the real problem was the system. Note it must match "couldn't find
# The Friend" (the title's own name), not only "couldn't find the title" -- a first version
# missed exactly that. "couldn't find out" is excluded: that is not blame.
_BLAME_TITLE = re.compile(
    r"(couldn'?t|could not|can'?t|cannot|didn'?t|did not|unable to|wasn'?t able to) find(?! out)\b|"
    r"does(n'?t| not) (exist|appear)|(isn'?t|is not) (available )?on (netflix|prime)|"
    r"no (results?|matches?) (for|found)|not (found|available) on", re.I)
_TV_PROBLEM = re.compile(r"\b(tv|television)\b[^.]{0,80}\b(off|respond|reach|connect|network|unavailable|asleep|standby|on)\b|"
                         r"\b(off|asleep|standby)\b", re.I)
# Advice that the tool result did not support: telling the user they may need to press Play
# after the tool reported that it HAD pressed Play. Heuristic: a modal/hedge word shortly
# before "press", or an "if nothing ..." hedge. Describing what was done ("...and press Play")
# does not match.
_UNSUPPORTED_ADVICE = re.compile(
    r"\b(need|needs|have|has|want|should|must|may|might|can|could|please|try|just|manually)\b[^.!?]{0,25}\bpress(ing)?\b|"
    r"\bif (nothing|it (doesn'?t|does not|didn'?t)|you (don'?t|do not|still don'?t))\b", re.I)
_UNVERIFIED = re.compile(r"(can'?t|cannot|unable to|not able to|couldn'?t)\s+(confirm|verify)|not verified|unverified", re.I)


def answer_says(rx):
    return lambda trace, answer: (bool(rx.search(answer)), "matched" if rx.search(answer) else "no match")


def answer_never_says(rx):
    def fn(trace, answer):
        m = rx.search(answer)
        return (m is None), (f"matched {m.group(0)!r}" if m else "clean")
    return fn


def asks_a_question(trace, answer):
    return "?" in answer, "contains a question" if "?" in answer else "no question mark"


def asks_which(trace, answer):
    ok = "?" in answer and bool(re.search(r"which|2025|2021|prime|netflix|year|service", answer, re.I))
    return ok, "asks which title" if ok else "does not ask which one"


def mentions_both_years(trace, answer):
    ok = "2025" in answer and "2021" in answer
    return ok, "names both years" if ok else "does not name both years"


def mentions_tv_state(trace, answer):
    ok = bool(re.search(r"\b(on|active|awake|powered|netflix)\b", answer, re.I))
    return ok, "mentions the state" if ok else "does not mention the state"


# ---------------------------------------------------------------- the cases

T = lambda name, fn: Check(name, fn)            # a hard check
S = lambda name, fn: Check(name, fn, soft=True)  # a soft check

CASES = [
    Case(1, "play The Friend 2025", "play The Friend 2025", "ok", "dryrun", [
        T("search_first", search_first),
        T("one_play", one_play),
        T("right_id", right_play("netflix", fx.THE_FRIEND_NETFLIX)),
        T("says_simulation", answer_says(_SIM)),
        T("no_playing_claim", answer_never_says(_CLAIM)),
        S("no_cheerleading", answer_never_says(_CHEER)),
    ]),
    Case(2, "title that does not exist", "play zzzxqq", "ok", "dryrun", [
        T("searched", searched),
        T("no_play", no_play),
        T("says_not_found", answer_says(_NOT_FOUND)),
    ]),
    Case(3, "ambiguous title (2 matches)", "game changer", "ok", "dryrun", [
        T("searched", searched),
        T("no_play", no_play),
        T("asks_which", asks_which),
        S("names_both_years", mentions_both_years),
    ]),
    Case(4, "title with a year", "game changer 2025", "ok", "dryrun", [
        T("one_play", one_play),
        T("right_id", right_play("prime", fx.GAME_CHANGER_2025_PRIME)),
        T("id_from_search", id_from_search),
        T("says_simulation", answer_says(_SIM)),
        S("search_used_year", search_used_year("2025")),
        S("no_cheerleading", answer_never_says(_CHEER)),
    ]),
    Case(5, "TV status question", "is the tv on?", "ok", "dryrun", [
        T("only_status_tool", only_status_tool),
        S("mentions_state", mentions_tv_state),
    ]),
    Case(6, "vague request", "play something good", "ok", "dryrun", [
        T("no_play", no_play),
        T("asks_a_question", asks_a_question),
        S("no_pointless_search", no_nonsense_search),
    ]),
    Case(7, "request with no matching tool", "pause the tv", "ok", "dryrun", [
        T("no_play", no_play),
        T("says_cannot", answer_says(_CANNOT)),
    ]),
    Case(8, "catalog rate limited", "play The Friend 2025", "rate_limited", "dryrun", [
        T("searched", lambda tr, a: (len(asked(tr, "search_title")) >= 1, "search attempted")),
        T("no_play", no_play),
        T("no_identical_retry", no_repeat_identical_search),
        T("explains_system_problem", answer_says(_SYSTEM_PROBLEM)),
        T("does_not_blame_title", answer_never_says(_BLAME_TITLE)),
    ]),
    Case(9, "TV unreachable", "play The Friend 2025", "ok", "unreachable", [
        T("one_play_attempt", one_play),
        T("got_tv_unreachable", got_tv_unreachable),
        T("no_success_claim", answer_never_says(_CLAIM)),
        T("explains_tv_problem", answer_says(_TV_PROBLEM)),
    ]),
    Case(10, "title on both services", "play Dual Title 2020", "ok", "dryrun", [
        T("one_play", one_play),
        T("valid_service_id_pair", valid_service_id_pair({("netflix", fx.DUAL_NETFLIX), ("prime", fx.DUAL_PRIME)})),
        T("id_from_search", id_from_search),
        S("no_cheerleading", answer_never_says(_CHEER)),
    ]),
    Case(11, "live success: no unsupported advice", "play The Friend 2025", "ok", "live", [
        T("one_play", one_play),
        T("right_id", right_play("netflix", fx.THE_FRIEND_NETFLIX)),
        T("live_success_seen", play_result_is_live_success),
        T("no_unsupported_advice", answer_never_says(_UNSUPPORTED_ADVICE)),
        S("admits_unverified", answer_says(_UNVERIFIED)),
        S("no_cheerleading", answer_never_says(_CHEER)),
    ], added_by_assistant=True),
]

CASES_BY_ID = {c.id: c for c in CASES}
