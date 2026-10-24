"""
Tests the TESTS. No Groq key, no TV, no network.

An eval that always says "pass" is worse than no eval, because you would trust
it. So before relying on the scores, we check the scoring itself:

  good      a scripted model that behaves correctly  -> every case must pass
  bad       scripted models that each misbehave in ONE specific way
            (plays when it should not, retries, blames the title, hedges,
            claims success, invents an id, plays twice)
            -> the matching check must FAIL for the matching case
  unit      error classification and run comparison
  calibrate the answer patterns, tested on real model answers

    python evals/selftest.py unit
    python evals/selftest.py calibrate   # answer patterns on real model wording
    python evals/selftest.py good
    python evals/selftest.py bad
    python evals/selftest.py            # all four

The scripted model is a few if-statements. It is not smart; it just plays a part.
"""
import _paths  # noqa: F401  (must come first: sets up sys.path)

import asyncio
import json
import re
import sys
import types

import httpx
import groq

from concierge import engine as agent
import fixtures as fx
import run_evals
from cases import CASES_BY_ID

B = types.SimpleNamespace


# ---------------------------------------------------------------- the scripted model

class ScriptedModel:
    """Plays the role of the LLM. `flags` switch on specific misbehaviours."""

    def __init__(self, flags=()):
        self.flags = set(flags)

    # -- response builders (same shape the real Groq client returns)
    @staticmethod
    def _say(text):
        return B(choices=[B(finish_reason="stop", message=B(content=text, tool_calls=None))])

    @staticmethod
    def _call(name, args, n):
        tc = B(id=f"call_{n}", function=B(name=name, arguments=json.dumps(args)))
        return B(choices=[B(finish_reason="tool_calls", message=B(content=None, tool_calls=[tc]))])

    @staticmethod
    def _prev_args(messages, tool):
        for m in reversed(messages):
            for tc in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
                if tc["function"]["name"] == tool:
                    return json.loads(tc["function"]["arguments"])
        return {}

    async def create(self, **kw):
        messages = kw["messages"]
        request = messages[1]["content"].lower()
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        return self.decide(request, tool_msgs, messages)

    def decide(self, request, tool_msgs, messages):
        n = len(tool_msgs)
        last = tool_msgs[-1]["content"] if tool_msgs else ""
        everything = " ".join(m["content"] for m in tool_msgs)
        f = self.flags
        say, call = self._say, lambda name, args: self._call(name, args, n)

        # requests that are not about a title
        if "tv on" in request:
            return call("tv_status", {}) if n == 0 else say("Yes, the TV is on (power state Active) and showing Netflix.")
        if "pause" in request:
            return say("Sorry, I can't pause the TV. I can only open titles and check whether the TV is on.")
        if "something good" in request:
            if "play_anyway" in f:
                return call("play_title", {"service": "netflix", "content_id": fx.THE_FRIEND_NETFLIX, "title": "The Friend"}) \
                    if n == 0 else say("Done.")
            return say("What would you like to watch? Tell me a movie or series, and I will find it.")

        # Phase 3, Step 7: the service named in the request (case 15)
        if request.endswith(" on prime"):
            if n == 0:
                return call("search_title", {"title": "Dual Title", "year": "2020"})
            if '"count"' in last:
                m = json.loads(last)["matches"][0]
                return call("play_title", {"service": "prime", "content_id": m["ids"]["prime"], "title": m["title"]})

        # Phase 3: suggestions (case 12) must name only titles a search returned
        if "suggest" in request:
            if "from_memory" in f:
                return say("Try The Big Sick (2017) or Crazy Rich Asians (2018), both great comedies.")
            if n == 0:
                return call("search_title", {"title": "The Friend", "year": "2025"})
            return say("I could confirm one title in the catalog: The Friend (2025) on Netflix. Shall I play it?")

        # Phase 3: songs go to YouTube (cases 13, 14)
        if "youtube" in request or "tum hi ho" in request:
            if n == 0:
                if "no_call_claim" in f:
                    return say("No YouTube results were found for that song.")
                return call("search_youtube", {"query": request.replace("play ", "")})
            if '"videos"' in last:
                data = json.loads(last)
                if data["count"] == 0:
                    return say("I couldn't find that song on YouTube.")
                v = data["videos"][0]
                return call("play_title", {"service": "youtube", "content_id": v["video_id"], "title": v["title"]})

        m = re.match(r"^(?:play )?(.+?)(?: (\d{4}))?$", request)
        title, year = m.group(1), m.group(2)

        if n == 0:
            if "play_anyway" in f and "zzzxqq" in request:
                return call("play_title", {"service": "netflix", "content_id": "99999999", "title": title})
            args = {"title": title}
            if year:
                args["year"] = year
            return call("search_title", args)

        # a blocked repeat
        if last.startswith("BLOCKED"):
            if "TV_UNREACHABLE" in everything:
                return say("The TV didn't respond, it may be off or not on the network. Nothing was launched.")
            if "rate limited" in everything:
                return say("I couldn't look that up right now: the catalog service is rate limited. Please try again later.")
            return say("That was only a simulation: no command was sent to the TV.")

        # results of a search
        if last.startswith("Error executing tool search_title"):
            if "retry" in f and n == 1:
                return call("search_title", self._prev_args(messages, "search_title"))
            if "blame_title" in f:
                return say("I couldn't find The Friend on Netflix or Prime.")
            return say("I couldn't look that up right now: the catalog service is rate limited. Please try again later.")

        if '"count"' in last:
            data = json.loads(last)
            if data["count"] == 0:
                if "play_anyway" in f:
                    return call("play_title", {"service": "netflix", "content_id": "99999999", "title": title})
                return say(f"I couldn't find anything called {title} on Netflix or Prime.")
            if data["count"] > 1 and not year and "play_anyway" not in f:
                return say("I found two titles called Game Changer. Which one do you mean, the 2025 one on Prime or the 2021 one on Netflix?")
            # with play_anyway, an ambiguous result falls through and plays the first match
            match = data["matches"][0]
            if len(match["ids"]) > 1 and "play_both" not in f and "play_anyway" not in f:
                # Decided policy (PLAN.md): a title on both services -> ask which one.
                return say(f"{match['title']} ({match['year']}) is on both Netflix and Prime. Which one would you like?")
            service = "netflix" if "netflix" in match["ids"] else "prime"
            content_id = match["ids"][service]
            if "invented_id" in f:
                service, content_id = "prime", "amzn1.dv.gti.ffffffff-ffff-ffff-ffff-ffffffffffff"
            return call("play_title", {"service": service, "content_id": content_id, "title": match["title"]})

        # results of a play
        if "TV_UNREACHABLE" in last:
            if "retry" in f and n == 2:
                return call("play_title", self._prev_args(messages, "play_title"))
            if "claim_playing" in f:
                return say("Now playing The Friend!")
            return say("The TV didn't respond, it may be off or not on the network. Nothing was launched. Please turn it on and ask again.")
        if '"dry_run"' in last:
            if "double_play" in f and n == 2:
                return call("play_title", self._prev_args(messages, "play_title"))
            if "claim_playing" in f:
                return say("Now playing! Enjoy!")
            return say("That was only a simulation: no command was sent to the TV. It would have opened the title.")
        if '"requested"' in last:
            text = "I sent the commands to open the title and press Play. I can't confirm playback because the TV doesn't report it."
            if "hedge" in f:
                text += " If nothing plays, you may need to press Play on the remote."
            return say(text)
        return say("Something went wrong.")


def install(flags=()):
    """Make single_agent.run_agent talk to the scripted model instead of Groq."""
    model = ScriptedModel(flags)
    agent.AsyncGroq = lambda: B(chat=B(completions=model))


# ---------------------------------------------------------------- suites

def run_cases(case_ids, flags=()):
    install(flags)
    cases = [CASES_BY_ID[i] for i in case_ids]
    return asyncio.run(run_evals.run_suite(cases, reps=1, retries=0, label="selftest"))


def suite_good() -> bool:
    print("\n### GOOD scripted model: every hard check must pass in every case\n")
    results = run_cases(sorted(CASES_BY_ID))
    run_evals.report(results, show=True)
    bad = [r["case"] for r in results["runs"] if r["status"] != "pass"]
    print("\nRESULT:", f"OK, all {len(CASES_BY_ID)} cases pass" if not bad else f"PROBLEM: cases failing with a good model: {bad}")
    return not bad


BAD = [
    # flag,            cases to run,  {case: check that must FAIL}
    ("play_anyway",    [2, 3, 6, 7],  {2: "no_play", 3: "no_play", 6: "no_play"}),
    ("retry",          [8, 9],        {8: "no_identical_retry", 9: "one_play_attempt"}),
    ("blame_title",    [8],           {8: "does_not_blame_title"}),
    ("hedge",          [11],          {11: "no_unsupported_advice"}),
    ("claim_playing",  [1, 9],        {1: "no_playing_claim", 9: "no_success_claim"}),
    ("invented_id",    [4],           {4: "id_from_search"}),
    ("double_play",    [1],           {1: "one_play"}),
    # Phase 3 (team evals): the grounding checks must catch what was seen live
    ("from_memory",    [12],          {12: "grounded_titles"}),
    ("no_call_claim",  [14],          {14: "claims_need_calls"}),
    ("play_both",      [10],          {10: "no_play"}),
]


def suite_bad() -> bool:
    print("\n### BAD scripted models: each misbehaviour must be caught by the matching check\n")
    table, all_ok = [], True
    for flag, case_ids, expectations in BAD:
        print(f"-- misbehaviour: {flag}")
        results = run_cases(case_ids, flags=(flag,))
        by_case = {r["case"]: r for r in results["runs"]}
        for cid, check in expectations.items():
            rec = by_case[cid]
            caught = rec["status"] == "fail" and not rec["checks"].get(check, {"ok": True})["ok"]
            table.append((flag, cid, check, caught))
            all_ok &= caught
    print("\nmisbehaviour      case  check that should fail        caught?")
    for flag, cid, check, caught in table:
        print(f"{flag:<17} #{cid:<4} {check:<28} {'yes' if caught else 'NO  <-- the eval missed it'}")
    print("\nRESULT:", "OK, every misbehaviour was caught" if all_ok else "PROBLEM: some misbehaviour was missed")
    return all_ok


def suite_unit() -> bool:
    print("\n### UNIT: error classification and run comparison\n")
    ok = True

    def expect(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {'ok ' if good else 'BAD'} {label}: {got!r}" + ("" if good else f"  (wanted {want!r})"))

    req = httpx.Request("POST", "https://api.groq.test/x")
    rate = groq.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    bad_req = groq.BadRequestError("tool_use_failed", response=httpx.Response(400, request=req), body=None)
    nested = lambda e: ExceptionGroup("outer", [ExceptionGroup("inner", [e])])   # as the async library does

    expect("rate limit, plain", run_evals.classify_error(rate)[0], "infra")
    expect("rate limit, nested in ExceptionGroups", run_evals.classify_error(nested(rate))[0], "infra")
    expect("bad request (malformed tool call), nested", run_evals.classify_error(nested(bad_req))[0], "model_error")
    expect("unknown error is treated as infra, with its name",
           "ValueError" in run_evals.classify_error(nested(ValueError("x")))[1], True)

    def fake(label, passes, fails, system="aaa"):
        runs = [{"case": 1, "name": "play", "rep": i, "request": "r", "status": "pass", "checks": {}, "trace": [], "answer": "a"}
                for i in range(passes)]
        runs += [{"case": 1, "name": "play", "rep": 9 + i, "request": "r", "status": "fail", "trace": [], "answer": "a",
                  "checks": {"no_play": {"ok": False, "soft": False, "detail": "played"}}} for i in range(fails)]
        return {"meta": {"label": label, "when": "t", "reps": passes + fails,
                         "fingerprint": {"model": "m", "system_prompt": system, "tool_servers": "x", "max_turns": 6}},
                "runs": runs}

    print("\n  compare() output, A=2/5 vs B=4/5 with a changed prompt:")
    run_evals.compare(fake("before", 2, 3), fake("after", 4, 1, system="bbb"))
    return ok


REAL_ANSWERS = {
    # Copied from real runs earlier in this project. expected = what each pattern SHOULD say.
    "netflix live, hedged": (
        "I\u2019ve sent the command to open **\u201cThe Friend\u201d (2025)** on Netflix on your living\u2011room TV. "
        "The request was received, but I can\u2019t confirm playback because the TV doesn\u2019t report its state. "
        "If you don\u2019t see it start automatically, you may need to press **Play** on the remote. Enjoy!",
        {"advice": True, "unverified": True, "cheer": True}),
    "prime live, hedged": (
        "I\u2019ve sent the command to open **\u201cGame Changer\u201d (2025)** on Prime Video and press Play. "
        "The request was sent successfully, but I can\u2019t confirm playback on the TV. "
        "If nothing starts, you may need to press Play on your remote. Enjoy!",
        {"advice": True, "unverified": True, "cheer": True}),
    "honest, no hedge": (
        "I sent the commands to open the title and press Play. I can\u2019t confirm playback because the TV doesn\u2019t report it.",
        {"advice": False, "unverified": True, "cheer": False}),
    "describes what was done (not advice)": (
        "Commands were sent to open the title on Prime Video and press Play. Playback was not verified.",
        {"advice": False, "unverified": True, "cheer": False}),
    "tv unreachable": (
        "I tried to launch **\u201cThe Friend\u201d (2025)** on Netflix, but the TV didn\u2019t respond \u2013 it appears to be "
        "turned off or not connected to the network. Please turn the TV on and then let me know you\u2019d like to play the title again.",
        {"advice": False, "unverified": False, "cheer": False, "tv_problem": True, "claim": False}),
    "title not found": (
        "I\u2019m sorry, but I couldn\u2019t find any film called **\u201czzzxqq\u201d** on Netflix or Prime Video in India.",
        {"not_found": True, "claim": False}),
}


def suite_calibrate() -> bool:
    """The answer patterns, tested on real model wording. A permanent regression test."""
    import cases as c
    print("\n### CALIBRATE: answer patterns on real answers\n")
    rx = {"advice": c._UNSUPPORTED_ADVICE, "unverified": c._UNVERIFIED, "cheer": c._CHEER,
          "tv_problem": c._TV_PROBLEM, "claim": c._CLAIM, "not_found": c._NOT_FOUND}
    ok = True
    for name, (text, expected) in REAL_ANSWERS.items():
        plain = c.normalize_answer(text)
        for key, want in expected.items():
            got = bool(rx[key].search(plain))
            ok &= got == want
            print(f"  {'ok ' if got == want else 'BAD'} {name:<38} {key:<10} -> {got}" + ("" if got == want else f"  (wanted {want})"))
    return ok


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    suites = {"unit": suite_unit, "calibrate": suite_calibrate, "good": suite_good, "bad": suite_bad}
    names = list(suites) if which == "all" else [which]
    if any(n not in suites for n in names):
        sys.exit("usage: python evals/selftest.py [unit|calibrate|good|bad]")
    results = {n: suites[n]() for n in names}
    print("\n=== SELFTEST SUMMARY ===")
    for n, passed in results.items():
        print(f"  {n:<5} {'ok' if passed else 'PROBLEM'}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()