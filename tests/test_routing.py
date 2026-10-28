"""
Routing, workflows and skills (Phase 3, Step 7).

    router    which route each request takes (rules), and that unclear requests are NOT guessed
    workflows the policies are CODE: they hold every time, with ZERO model calls (a model that
              raises if called proves it); the fake catalog and fake TV from the evals (stdio)
    skills    the skill files load, and a broken file is refused with a clear message
    fallback  anything the router cannot place goes to the Phase 3 team

No Groq key, no Keycloak, no TV.
Run:  python tests/test_routing.py        (or: pytest tests)
"""
import asyncio
import contextlib
import io
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from concierge import engine as agent
from common import config
from concierge import router
from baselines import routed
from concierge import skills
from test_agent_recovery import reply
from test_multi_agent import RecordingGroq

FIXED = {"catalog": "evals/srv_catalog_ok.py", "device": "evals/srv_device_dryrun.py"}
FRIEND, DUAL_PRIME, SONG = "82182295", "amzn1.dv.gti.11111111-2222-3333-4444-555555555555", "Umqb9KENgmk"


class NoModel:
    """A 'model' that fails the test if anyone calls it."""
    def __init__(self):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        raise AssertionError("a model was called, but this route must need none")


def run(request, servers=None, model=NoModel, router_model=""):
    saved = agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL
    agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL = model, dict(servers or FIXED), router_model, ""
    trace = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            answer = asyncio.run(routed.run_routed(request, trace))
    finally:
        agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL = saved
    return answer, trace


def calls(trace, tool):
    return [t for t in trace if t["tool"] == tool]


# ----------------------------------------------------------------- router
ROUTING_TABLE = [
    ("play The Friend 2025", "PLAY", {"title": "The Friend", "year": "2025"}),
    ("watch Game Changer (2021)", "PLAY", {"title": "Game Changer", "year": "2021"}),
    ("play Dual Title 2020 on prime", "PLAY", {"title": "Dual Title", "year": "2020", "service": "prime"}),
    ("play the diplomat tv series", "PLAY", {"title": "the diplomat"}),          # no year invented
    ("play tum hi ho by arijit singh", "SONG", {"query": "tum hi ho by arijit singh"}),
    ("play the song zzzxqq on youtube", "SONG", {"query": "zzzxqq"}),
    ("play taaki taaki rumba on you tube", "SONG", {"query": "taaki taaki rumba"}),   # seen live: "you tube"
    ("play kesariya on YT", "SONG", {"query": "kesariya"}),
    ("is the tv on?", "STATUS", {}),
    ("I don't like horror, and I love Christopher Nolan films", "TASTE", {}),
    ("share my taste with the household", "TASTE", {}),
    ("me and Mishika want to watch something tonight", "SUGGEST", {"also_taste": False}),
    ("I like comedy, can you suggest a few titles", "SUGGEST", {"also_taste": True}),
    # polite wrappers (seen live: "can you play lupin?" cost a router-model call)
    ("can you play lupin?", "PLAY", {"title": "lupin"}),
    ("could you please put on The Friend 2025 for me", "PLAY", {"title": "The Friend", "year": "2025"}),
    ("please play Dual Title 2020 on prime, thanks", "PLAY", {"title": "Dual Title", "year": "2020", "service": "prime"}),
    ("hey, can you play tum hi ho by arijit singh please", "SONG", {"query": "tum hi ho by arijit singh"}),
    ("i want to watch Game Changer", "PLAY", {"title": "Game Changer"}),
]


def test_rules_route_clear_requests():
    for request, kind, params in ROUTING_TABLE:
        r = router.by_rules(request)
        assert r is not None and r.kind == kind, (request, r)
        assert r.params == params, (request, r.params)


def test_unclear_requests_are_not_guessed():
    for request in ("taaki taaki rumba", "game changer", "pause the tv", "play something good"):
        r = router.by_rules(request)
        assert r is None or r.kind not in ("PLAY", "SONG"), (request, r)


def test_without_a_router_model_unclear_means_fallback():
    saved, config.ROUTER_MODEL = config.ROUTER_MODEL, ""
    try:
        r = asyncio.run(router.route("pause the tv"))
    finally:
        config.ROUTER_MODEL = saved
    assert (r.kind, r.source) == ("FALLBACK", "default"), r


def test_router_model_places_what_the_rules_cannot():
    # "taaki taaki rumba": no rule matches; ONE call to the small model says SONG; the song workflow runs.
    fake = RecordingGroq([reply(text='{"route": "SONG", "title": "tum hi ho"}')])
    answer, trace = run("taaki taaki rumba", model=lambda: fake, router_model="small-model")
    assert trace[0]["args"] == {"route": "SONG", "source": "model", "reason": "classified by the router model"}
    assert len(fake.calls) == 1 and calls(trace, "play_title")[0]["args"]["service"] == "youtube"


def test_router_model_play_is_parsed_like_the_rules():
    # Seen in the evals (case 4): the model said PLAY, title "game changer 2025": the year must be split off.
    fake = RecordingGroq([reply(text='{"route": "PLAY", "title": "game changer 2025"}')])
    _, trace = run("game changer 2025", model=lambda: fake, router_model="small-model")
    assert calls(trace, "search_title")[0]["args"] == {"title": "game changer", "year": "2025"}
    assert calls(trace, "play_title")[0]["args"]["service"] == "prime"          # the 2025 one is on Prime
    # ...and a year the MODEL invents (not in the person's words) is dropped.
    fake = RecordingGroq([reply(text='{"route": "PLAY", "title": "game changer 2021"}')])
    _, trace = run("game changer", model=lambda: fake, router_model="small-model")
    assert calls(trace, "search_title")[0]["args"] == {"title": "game changer"}


def test_router_model_dropping_a_typed_year_does_not_lose_it():
    # Seen in the Phase 4 evals (case 4, 0/3): the model said PLAY with title "game changer" for
    # "game changer 2025". The typed year must still reach the search (else: 2 matches -> asks).
    for title in ("game changer", "Game Changer"):
        fake = RecordingGroq([reply(text='{"route": "PLAY", "title": "%s"}' % title)])
        _, trace = run("game changer 2025", model=lambda: fake, router_model="small-model")
        assert calls(trace, "search_title")[0]["args"] == {"title": title, "year": "2025"}, title
        assert calls(trace, "play_title")[0]["args"]["service"] == "prime"


def test_a_failing_router_model_means_fallback_not_a_crash():
    # Seen live: ROUTER_MODEL named a model the key could not use -> 404 on every request.
    import httpx
    from groq import NotFoundError
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    not_found = NotFoundError("model not found", response=httpx.Response(404, request=request),
                              body={"error": {"message": "model not found"}})
    fake = RecordingGroq([not_found, reply(text="Sorry, I am not sure what you mean.")])

    async def create(**kw):                       # first call (router) raises, second (team) answers
        item = fake.script.pop(0)
        fake.calls.append({"tools": [], "messages": kw["messages"]})
        if isinstance(item, Exception):
            raise item
        return item
    fake.chat.completions.create = create
    answer, trace = run("taaki taaki rumba", model=lambda: fake, router_model="no-such-model")
    assert trace[0]["args"]["route"] == "FALLBACK" and "router model failed" in trace[0]["args"]["reason"]
    assert answer == "Sorry, I am not sure what you mean."


def test_a_confused_router_model_means_fallback():
    for text in ("I think it is a song", '{"route": "DANCE"}'):
        fake = RecordingGroq([reply(text=text), reply(text="Sorry, I am not sure what you mean.")])
        _, trace = run("taaki taaki rumba", model=lambda: fake, router_model="small-model")
        assert trace[0]["args"]["route"] == "FALLBACK", (text, trace[0])


# ----------------------------------------------------------------- workflows: policies as code, 0 calls
def test_play_one_match_plays_it():
    answer, trace = run("play The Friend 2025")
    assert trace[0]["args"]["route"] == "PLAY"
    assert calls(trace, "search_title")[0]["args"] == {"title": "The Friend", "year": "2025"}
    assert calls(trace, "play_title")[0]["args"]["content_id"] == FRIEND           # by code, not a model
    assert "simulation" in answer and "playing" not in answer.lower()


def test_a_polite_request_costs_no_model_call():
    # NoModel raises if anything calls a model: the rules must place it, as for "play lupin".
    _, trace = run("can you please play The Friend 2025 for me?")
    assert trace[0]["args"]["route"] == "PLAY" and trace[0]["args"]["source"] == "rule"
    assert calls(trace, "play_title")[0]["args"]["content_id"] == FRIEND


def test_polite_wrapping_never_changes_the_route():
    for plain in ("play something good", "play lupin", "is the tv on?", "play tum hi ho by arijit singh"):
        polite = f"hey, can you please {plain.rstrip('?')} for me?"
        assert router.by_rules(polite) == router.by_rules(plain), (polite, plain)


def test_one_exact_title_among_partial_matches_is_played():
    # Seen live: "lupin" found "Lupin (2021)" and "LUPIN THE 3rd vs. CAT'S EYE (2023)". Exactly one is the
    # title asked for: play it, and say what else was found.
    from evals.fixtures import LUPIN_NETFLIX
    answer, trace = run("play lupin")
    p = calls(trace, "play_title")[0]["args"]
    assert (p["service"], p["content_id"]) == ("netflix", LUPIN_NETFLIX)
    assert "Lupin (2021)" in answer and "also found" in answer and "CAT'S EYE" in answer


def test_exact_match_ignores_case_and_punctuation_only():
    from concierge.workflows import same_title
    assert same_title("LUPIN", "lupin!") and same_title("Lupin ", "lupin")
    assert not same_title("LUPIN THE 3rd", "lupin") and not same_title("The Friend", "Friend")


def test_several_exact_matches_still_ask():
    answer, trace = run("play Game Changer")             # 2021 and 2025: both exactly "Game Changer"
    assert not calls(trace, "play_title") and "Which one" in answer


def test_the_note_is_not_added_to_a_failure():
    answer, _ = run("play lupin", {**FIXED, "device": "evals/srv_device_unreachable.py"})
    assert "didn't respond" in answer and "also found" not in answer


def test_no_year_is_invented():
    _, trace = run("play The Friend")
    assert calls(trace, "search_title")[0]["args"] == {"title": "The Friend"}


def test_both_services_and_none_named_asks():
    answer, trace = run("play Dual Title 2020")
    assert not calls(trace, "play_title") and "Which one" in answer and "Netflix" in answer and "Prime" in answer


def test_named_service_is_used():
    _, trace = run("play Dual Title 2020 on prime")
    p = calls(trace, "play_title")[0]["args"]
    assert (p["service"], p["content_id"]) == ("prime", DUAL_PRIME)


def test_several_titles_asks_which():
    answer, trace = run("play Game Changer")
    assert not calls(trace, "play_title") and "2025" in answer and "2021" in answer and "?" in answer


def test_not_found_says_so():
    answer, trace = run("play zzzxqq")
    assert not calls(trace, "play_title") and "couldn't find" in answer


def test_system_problem_is_not_blamed_on_the_title():
    answer, _ = run("play The Friend 2025", {**FIXED, "catalog": "evals/srv_catalog_rate_limited.py"})
    assert "couldn't find" not in answer and "try again later" in answer


def test_tv_unreachable_is_explained():
    answer, trace = run("play The Friend 2025", {**FIXED, "device": "evals/srv_device_unreachable.py"})
    assert len(calls(trace, "play_title")) == 1 and "didn't respond" in answer and "Nothing was launched" in answer


def test_song_plays_the_first_video():
    answer, trace = run("play tum hi ho by arijit singh")
    p = calls(trace, "play_title")[0]["args"]
    assert (p["service"], p["content_id"]) == ("youtube", SONG) and "simulation" in answer


def test_tv_status_says_why_the_tv_is_not_reachable():
    # Seen live: "isn't answering" while the TV was on (the PC was on another Wi-Fi). The answer now carries the
    # reason. (On a PC with TV_IP set, the fake server also probes that address: either wording is fine.)
    answer, trace = run("is the tv on?", {**FIXED, "device": "evals/srv_device_unreachable.py"})
    s = json.loads(calls(trace, "tv_status")[0]["result"])
    assert s["reachable"] is False and "no route to host" in s["error"], s
    assert "isn't answering" in answer and "no route to host" in answer, answer


def test_tv_status_uses_only_tv_status():
    answer, trace = run("is the tv on?")
    assert [t["tool"] for t in trace if not t["tool"].startswith("(")] == ["tv_status"]
    assert answer.startswith("Yes, the TV is on")


# ----------------------------------------------------------------- fallback
def test_unclear_request_goes_to_the_team():
    fake = RecordingGroq([reply(text="Sorry, I can't pause the TV.")])          # the Supervisor's one call
    answer, trace = run("pause the tv", model=lambda: fake)
    assert trace[0]["args"]["route"] == "FALLBACK" and answer == "Sorry, I can't pause the TV."
    assert "ask_catalog_agent" in fake.calls[0]["tools"]                        # it was the team


def test_taste_route_without_a_taste_server_falls_back():
    fake = RecordingGroq([reply(text="I can't save preferences right now.")])
    _, trace = run("I don't like horror", model=lambda: fake)
    assert trace[0]["args"]["route"] == "TASTE" and len(fake.calls) == 1         # handled by the team


# ----------------------------------------------------------------- history titles
def test_long_titles_are_shortened_to_fit_the_history():
    # Seen live: a 120-character YouTube title was refused by record_watch (limit 100).
    long = ("Darmiyaan (Full Video) | Musafir Cafe | Vikrant Massey,Vedika,Mahima,Rekha Bhardwaj,"
            "Raghav K,Amrita (T-Series)")
    short = agent.history_title(long)
    assert len(short) <= 100 and short.endswith("…") and short.startswith("Darmiyaan (Full Video) | Musafir Cafe")
    assert agent.history_title("  The   Friend ") == "The Friend"              # short ones unchanged


# ----------------------------------------------------------------- skills
def test_skill_files_load():
    taste, suggest = skills.load("taste"), skills.load("suggest")
    assert taste.servers == ("taste",) and "get_household_taste" in taste.hide
    assert suggest.offer == {"get_household_taste"} and suggest.scopes == {"taste": ("taste-household-read",)}
    assert suggest.local_tools == ("check_titles",) and suggest.sha


def test_a_broken_skill_file_is_refused_clearly():
    bad = [
        ("no header", "You are ..."),
        ("not json", '---\nname: taste\n---\nbody'),
        ("missing servers", '---\nname: "x"\ndescription: "d"\n---\nbody'),
        ("offer and hide", '---\nname: "x"\ndescription: "d"\nservers: []\noffer: []\nhide: []\n---\nb'),
    ]
    for label, text in bad:
        try:
            skills.parse(text, label)
        except skills.SkillError as e:
            assert label.split()[0] in str(e) or "must" in str(e) or "JSON" in str(e) or "missing" in str(e), e
            continue
        raise AssertionError(f"{label}: accepted a broken skill file")


def test_a_skill_needing_an_unconfigured_server_says_so():
    try:
        skills.build_agent(skills.load("taste"), "taste", {"catalog": "x.py"}, {})
    except skills.SkillError as e:
        assert "not configured" in str(e)
        return
    raise AssertionError("built an agent without its server")


# ----------------------------------------------------------------- suggestions: how many titles (2026-10-28)
def test_check_titles_checks_up_to_12_titles_4_at_a_time():
    # "Give me 10 comedies" got 3-4: check_titles took only 4. Now 12, but never more than 4 searches at once.
    from concierge import skill_agents
    running, peak, real = [0], [0], agent.call_traced

    async def counting(box, name, args, trace, who):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        try:
            await asyncio.sleep(0.05)                   # long enough for parallel calls to overlap
            return await real(box, name, args, trace, who)
        finally:
            running[0] -= 1

    saved = agent.SERVERS, agent.call_traced
    agent.SERVERS, agent.call_traced = dict(FIXED), counting
    trace = []
    try:
        titles = [{"title": "The Friend", "year": "2025"}] + [{"title": f"Nope {i}"} for i in range(12)]   # 13
        with contextlib.redirect_stdout(io.StringIO()):
            result = json.loads(asyncio.run(skill_agents.check_titles_tool(trace).fn({"titles": titles})))
    finally:
        agent.SERVERS, agent.call_traced = saved
    assert len(calls(trace, "search_title")) == 12, len(calls(trace, "search_title"))   # the 13th is dropped
    assert peak[0] == 4, peak[0]
    assert [m["title"] for m in result["confirmed"]] == ["The Friend"] and len(result["not_found"]) == 11


def test_the_suggest_skill_proposes_more_when_a_number_is_asked():
    text = skills.load("suggest").instructions
    assert "one and a half times that many" in text and "at most 12" in text
    assert "you asked for 10; I could confirm 7" in text       # a shortfall is said, not hidden

if __name__ == "__main__":
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {name}  {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)