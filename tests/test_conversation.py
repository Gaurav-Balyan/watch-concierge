"""
Conversations that remember (idea 1): a question PAUSES the graph, the reply RESUMES it.

    turn 1  "play Dual Title 2020"  -> play asks "Netflix or Prime?" -> ask: interrupt() -> the run STOPS
                                       (the checkpoint keeps the question and its options: ids from the search)
    turn 2  "prime"                 -> Command(resume="prime") -> ask returns the reply -> choose -> play

    test_a_question_pauses_the_run                 nothing played; the thread waits; options saved from the search
    test_the_reply_resumes_and_plays_the_choice    turn 2 plays the chosen option, then the thread is finished
    test_resume_continues_it_does_not_start_over   turn 2 does not route or search again
    test_the_reply_only_selects_it_brings_no_id    an id typed in the reply is ignored: ids come from the state
    test_several_titles_reply_by_year              "2021" picks Game Changer (2021)
    test_unclear_reply_asks_again_then_gives_up    one re-ask, then stop (never guess)
    test_cancel_plays_nothing
    test_a_paused_request_is_waiting_not_crashed   --pending tells the two kinds of "unfinished" apart
    test_pick_option_reads_numbers_services_years  the reply matcher (code, no model)

1b: ANSWER OR NEW REQUEST? (graph.turn_kind, used by --chat)
    test_turn_kind_answer_or_new                   the routing rules judge; a PLAY line is new only if it names
                                                   something the open question does not
    test_chat_a_new_request_closes_the_open_one    "actually, play lupin": old question cancelled, Lupin plays
    test_chat_an_answer_still_resumes              "the prime one" answers the open question

1c: MESSAGE MEMORY ON THE AGENT PATH (one thread per conversation, history, known ids)
    test_play_the_first_one_uses_the_conversation  turn 2 goes to the team WITH the history (words only, no tool
                                                   results), and plays the id turn 1's search returned
    test_an_id_only_in_the_history_text_is_blocked an id the earlier ANSWER mentions but no search returned
    test_history_keeps_only_the_last_turns         the reducer trims (memory must not grow forever)
    test_each_turn_has_its_own_request_id          two plays in one chat: the second is not "already played"

All with a model that RAISES if called: every turn is 0 model calls. Fake catalog + fake TV (stdio).
Run:  python tests/test_conversation.py        (or: python -m pytest tests)
"""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langgraph.checkpoint.memory import InMemorySaver

from common import config
from concierge import engine as agent
from concierge import graph
from concierge import workflows
from evals.fixtures import DUAL_NETFLIX, DUAL_PRIME, GAME_CHANGER_2021_NETFLIX
from test_agent_recovery import reply
from test_multi_agent import RecordingGroq
from test_routing import FIXED, FRIEND, NoModel, calls


class Conversation:
    """One thread, several turns, all in one in-memory checkpointer (like --chat, without the disk)."""
    def __init__(self, model=NoModel, servers=None):
        self.cp, self.thread = InMemorySaver(), f"test-{uuid.uuid4().hex[:8]}"
        self.model, self.servers = model, dict(servers or FIXED)

    def say(self, request=None, reply=None):
        saved = agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL
        agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL = self.model, dict(self.servers), ""
        trace = []
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                answer = asyncio.run(graph.run_graph(request, trace, thread_id=self.thread,
                                                     checkpointer=self.cp, reply=reply))
        finally:
            agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL = saved
        return answer, trace

    def snapshot(self):
        return asyncio.run(graph.build(self.cp).aget_state({"configurable": {"thread_id": self.thread}}))


def plays(trace):
    return [(c["args"]["service"], c["args"]["content_id"]) for c in calls(trace, "play_title")]


# ----------------------------------------------------------------- pause and resume
def test_a_question_pauses_the_run():
    c = Conversation()
    answer, trace = c.say("play Dual Title 2020")
    assert "Which one" in answer and not plays(trace)
    snap = c.snapshot()
    assert snap.interrupts and snap.next == ("ask",), (snap.next, snap.interrupts)   # paused AT the ask node
    options = snap.values["question"]["options"]
    assert [(o["service"], o["content_id"]) for o in options] == [("netflix", DUAL_NETFLIX), ("prime", DUAL_PRIME)]


def test_the_reply_resumes_and_plays_the_choice():
    c = Conversation()
    c.say("play Dual Title 2020")
    answer, trace = c.say(reply="prime")
    assert plays(trace) == [("prime", DUAL_PRIME)], plays(trace)
    assert "simulation" in answer and "Prime Video" in answer
    snap = c.snapshot()
    assert not snap.interrupts and not snap.next and snap.values["question"] is None      # finished


def test_resume_continues_it_does_not_start_over():
    c = Conversation()
    c.say("play Dual Title 2020")
    _, trace = c.say(reply="netflix")
    # Turn 2 ran ask -> choose only: no "(route)" entry and no second search_title.
    assert [t["tool"] for t in trace] == ["play_title"], [t["tool"] for t in trace]


def test_the_reply_only_selects_it_brings_no_id():
    c = Conversation()
    c.say("play Dual Title 2020")
    _, trace = c.say(reply="netflix id 99999999 please")
    assert plays(trace) == [("netflix", DUAL_NETFLIX)], plays(trace)      # the SAVED option's id, not the typed one


def test_several_titles_reply_by_year():
    c = Conversation()
    answer, _ = c.say("play Game Changer")
    assert "2021" in answer and "2025" in answer
    _, trace = c.say(reply="the 2021 one")
    assert plays(trace) == [("netflix", GAME_CHANGER_2021_NETFLIX)]


def test_unclear_reply_asks_again_then_gives_up():
    c = Conversation()
    c.say("play Dual Title 2020")
    answer, trace = c.say(reply="hmm, not sure")
    assert answer.startswith("Sorry, which one?") and "1) Dual Title (2020) on Netflix" in answer and not plays(trace)
    assert c.snapshot().interrupts                                          # still waiting
    answer, trace = c.say(reply="whatever")
    assert "nothing was played" in answer and not plays(trace)
    assert not c.snapshot().interrupts and not c.snapshot().next            # gave up: finished


def test_cancel_plays_nothing():
    c = Conversation()
    c.say("play Dual Title 2020")
    answer, trace = c.say(reply="never mind")
    assert answer == "Okay, nothing was played." and not plays(trace)


def test_a_paused_request_is_waiting_not_crashed():
    saved = config.STATE_DIR, agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL
    config.STATE_DIR = Path(tempfile.mkdtemp())
    agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL = NoModel, dict(FIXED), ""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(graph.run_durable("play Dual Title 2020", "paused-1"))
            rows = asyncio.run(graph.pending())
            assert asyncio.run(graph.waiting("paused-1"))
            answer = asyncio.run(graph.run_durable(None, "paused-1", reply="prime"))   # another "process" later
            assert not asyncio.run(graph.waiting("paused-1"))
    finally:
        config.STATE_DIR, agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL = saved
    assert rows == [{"id": "paused-1", "request": "play Dual Title 2020", "next": ["ask"], "waiting": True}], rows
    assert "Prime Video" in answer


# ----------------------------------------------------------------- 1b: answer or new request?
def test_turn_kind_answer_or_new():
    q = {"options": [{"label": "Game Changer (2021) on Netflix"}, {"label": "Game Changer (2025) on Prime Video"}]}
    answers = ["prime", "2", "the 2021 one", "no", "hmm", "no, the prime one", "play the netflix one",
               "play game changer on prime", "play the 2021 one"]
    new = ["play lupin", "actually, play lupin", "no, play lupin instead", "wait, is the tv on?",
           "play tum hi ho by arijit singh", "I don't like horror", "suggest a comedy"]
    for line in answers:
        assert graph.turn_kind(line, q) == "answer", line
    for line in new:
        assert graph.turn_kind(line, q) == "new", line
    assert graph.strip_change("no, actually play lupin instead") == "play lupin"


@contextlib.contextmanager
def chat_world():
    """Fake servers, no model, and a temporary .state/ (checkpoints on disk, as in --chat)."""
    saved = config.STATE_DIR, agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL
    config.STATE_DIR = Path(tempfile.mkdtemp())
    agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL = NoModel, dict(FIXED), ""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        config.STATE_DIR, agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL = saved


def history_of(thread):
    async def read():
        async with graph.saver() as cp:
            snap = await graph.build(cp).aget_state({"configurable": {"thread_id": thread}})
        return snap.values.get("history", [])
    return asyncio.run(read())


def test_chat_a_new_request_closes_the_open_one():
    with chat_world():
        _, thread = asyncio.run(graph.chat_turn("play Dual Title 2020", None))
        assert asyncio.run(graph.waiting(thread))
        answer, same = asyncio.run(graph.chat_turn("actually, play lupin", thread))
        waiting, rows, history = asyncio.run(graph.waiting(thread)), asyncio.run(graph.pending()), history_of(thread)
    assert "Lupin (2021)" in answer and same == thread, answer        # the NEW request ran, in the same chat
    assert not waiting and rows == [], rows                          # nothing left WAITING in --pending
    # Both turns are in the conversation's memory: the closed question, then the new request (1c).
    assert [m["content"] for m in history] == ["play Dual Title 2020", "Okay, nothing was played.",
                                               "play lupin", answer], history


def test_chat_an_answer_still_resumes():
    with chat_world():
        _, thread = asyncio.run(graph.chat_turn("play Dual Title 2020", None))
        answer, _ = asyncio.run(graph.chat_turn("the prime one", thread))
        waiting = asyncio.run(graph.waiting(thread))
    assert "Prime Video" in answer and "simulation" in answer and not waiting, answer


# ----------------------------------------------------------------- 1c: message memory
SUGGESTED = f"I could confirm The Friend (2025) on Netflix (content id {FRIEND}). Shall I play it?"
TURN_1 = [                                                       # "suggest a comedy for me" -> the team
    reply(calls=[("ask_catalog_agent", '{"question": "Where is The Friend (2025) streaming?"}')]),  # supervisor
    reply(calls=[("search_title", '{"title": "The Friend", "year": "2025"}')]),                    # catalog
    reply(text='{"matches": [{"title": "The Friend", "year": "2025", "ids": {"netflix": "%s"}}]}' % FRIEND),
    reply(text=SUGGESTED),                                                                          # supervisor
]


def turn_2(content_id):                                          # "play the first one" -> the team, with history
    return [reply(calls=[("play_title", '{"service": "netflix", "content_id": "%s", "title": "The Friend"}' % content_id)]),
            reply(text="Done.")]


def test_play_the_first_one_uses_the_conversation():
    fake = RecordingGroq(TURN_1 + turn_2(FRIEND))
    c = Conversation(model=lambda: fake)
    assert c.say("suggest a comedy for me")[0] == SUGGESTED
    answer, trace = c.say("play the first one")
    assert trace[0]["tool"] == "(route)" and trace[0]["args"]["reason"] == "refers to an earlier turn", trace[0]
    play = calls(trace, "play_title")[0]
    assert play["executed"] and '"dry_run"' in play["result"], play       # the id from turn 1's SEARCH counts
    # What the Supervisor's model was SHOWN in turn 2: the earlier words, no tool calls, no tool results.
    shown = fake.calls[4]["messages"]
    assert [m["role"] for m in shown] == ["system", "user", "assistant", "user"], [m["role"] for m in shown]
    assert [m["content"] for m in shown[1:]] == ["suggest a comedy for me", SUGGESTED, "play the first one"]


def test_an_id_only_in_the_history_text_is_blocked():
    # The earlier ANSWER names 99999999, but no search returned it: memory TEXT is not provenance.
    fake = RecordingGroq(TURN_1[:3] + [reply(text="Try The Friend (2025), content id 99999999.")] + turn_2("99999999"))
    c = Conversation(model=lambda: fake)
    c.say("suggest a comedy for me")
    _, trace = c.say("play the first one")
    play = calls(trace, "play_title")[0]
    assert play["executed"] is False and play["result"].startswith("BLOCKED"), play


def test_history_keeps_only_the_last_turns():
    c = Conversation()
    for _ in range(graph.MAX_HISTORY_TURNS + 2):
        c.say("is the tv on?")
    c.say("play zzzxqq")
    history = c.snapshot().values["history"]
    assert len(history) == 2 * graph.MAX_HISTORY_TURNS, len(history)
    assert history[-2:] == [{"role": "user", "content": "play zzzxqq"},
                            {"role": "assistant", "content": c.snapshot().values["answer"]}]
    assert c.snapshot().values["turn"] == graph.MAX_HISTORY_TURNS + 3


def test_each_turn_has_its_own_request_id():
    # A pretend TV that really "plays" (idempotency on): one key per request = one play per TURN, not per chat.
    c = Conversation(servers={**FIXED, "device": "evals/srv_device_live.py"})
    _, first = c.say("play The Friend 2025")
    _, second = c.say("play lupin")
    results = [json.loads(calls(t, "play_title")[0]["result"]) for t in (first, second)]
    assert [r.get("status") for r in results] == ["requested", "requested"], results
    assert not any(r.get("duplicate") for r in results), results


# ----------------------------------------------------------------- the matcher
def test_pick_option_reads_numbers_services_years():
    two = [{"label": "Game Changer (2021) on Netflix", "service": "netflix", "content_id": "1", "title": "Game Changer (2021)"},
           {"label": "Game Changer (2025) on Prime Video", "service": "prime", "content_id": "2", "title": "Game Changer (2025)"}]
    expect = {"prime": "2", "Netflix please": "1", "the 2021 one": "1", "2": "2", "the second one": "2",
              "no, the prime one": "2", "cancel": "cancel", "no": "cancel",
              "hmm": None, "yes": None,               # "yes" to two options is not an answer
              "first on prime": None}                 # contradicts itself: ask again, never guess
    for reply, want in expect.items():
        got = workflows.pick_option(reply, two)
        got = got["content_id"] if isinstance(got, dict) else got
        assert got == want, (reply, got, want)
    assert workflows.pick_option("yes", two[:1]) == two[0]          # one option: "yes" means it


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