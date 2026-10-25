"""
The Telegram bot front end (concierge/telegram_bot.py), against a FAKE Telegram (an httpx mock transport that answers
getUpdates / sendMessage / getFile / file downloads like the Bot API). No network, no token, no Groq.

    test_an_allowed_message_gets_the_concierges_answer   text -> chat_turn -> sendMessage; the thread is saved
    test_a_stranger_is_ignored_silently                  not in TELEGRAM_ALLOWED_IDS: no reply, no agent run
    test_a_voice_note_is_heard_then_answered             download -> speech-to-text -> "I heard: ..." -> the answer
    test_one_chat_is_one_conversation_and_new_resets     same thread for the next message; /new starts over
    test_the_offset_moves_on                             handled messages are never fetched (or handled) again
    test_groups_are_ignored                              private chats only
    test_a_login_problem_is_explained                    the bot keeps running and says what to do
    test_end_to_end_a_question_and_its_answer            the REAL graph (fake TV + catalog, no model): asks, then plays

Run:  python tests/test_telegram.py        (or: python -m pytest tests)
"""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from common import config
from common import login
from concierge import engine as agent
from concierge import telegram_bot
from test_routing import FIXED, NoModel

TOKEN = "123:TEST"
ME, STRANGER = 1111, 9999


class FakeTelegram:
    """Answers like the Bot API. `updates` is served once by getUpdates; every call is recorded."""
    def __init__(self, updates=()):
        self.updates, self.calls = list(updates), []
        self.transport = httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(f"/file/bot{TOKEN}/"):
            return httpx.Response(200, content=b"OggS-fake-voice")
        method = path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            served, self.updates = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)], []
            return httpx.Response(200, json={"ok": True, "result": served})
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_7.oga"}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    def sent(self):
        return [b["text"] for m, b in self.calls if m == "sendMessage"]


def message(update_id, text=None, user=ME, chat_type="private", voice=False):
    msg = {"message_id": update_id, "from": {"id": user, "username": f"u{user}"},
           "chat": {"id": user, "type": chat_type}}
    if voice:
        msg["voice"] = {"file_id": "VOICE1", "duration": 2}
    else:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


def make_bot(fake, turn=None, transcribe=None):
    async def default_turn(text, thread):
        return f"answer to {text!r}", thread or "thread-A"

    async def default_transcribe(audio, name):
        return "play tum hi ho by arijit singh"

    http = httpx.AsyncClient(transport=fake.transport)
    bot = telegram_bot.TelegramBot(TOKEN, [str(ME)], http, turn=turn or default_turn,
                                   transcribe=transcribe or default_transcribe,
                                   threads_file=Path(tempfile.mkdtemp()) / "threads.json")
    bot.fake = fake
    return bot


def run(bot, *updates):
    bot.fake.updates.extend(updates)                                # queue them on the fake Telegram
    with contextlib.redirect_stdout(io.StringIO()) as out:
        asyncio.run(bot.poll_once())
    return out.getvalue()


# ----------------------------------------------------------------- tests
def test_an_allowed_message_gets_the_concierges_answer():
    fake = FakeTelegram()
    bot = make_bot(fake)
    run(bot, message(1, "is the tv on?"))
    assert fake.sent() == ["answer to 'is the tv on?'"], fake.sent()
    assert bot.threads == {str(ME): "thread-A"}
    assert json.loads(bot.threads_file.read_text()) == {str(ME): "thread-A"}        # survives a restart


def test_a_stranger_is_ignored_silently():
    turns = []

    async def turn(text, thread):
        turns.append(text)
        return "x", "t"
    fake = FakeTelegram()
    bot = make_bot(fake, turn=turn)
    out = run(bot, message(1, "play something", user=STRANGER), message(2, "again", user=STRANGER))
    assert fake.sent() == [] and turns == [], (fake.sent(), turns)                     # no reply, no agents
    assert out.count(f"IGNORED a message from id {STRANGER}") == 1, out                # told to the owner once


def test_a_voice_note_is_heard_then_answered():
    heard = []

    async def transcribe(audio, name):
        heard.append((audio, name))
        return "play tum hi ho by arijit singh"
    fake = FakeTelegram()
    bot = make_bot(fake, transcribe=transcribe)
    run(bot, message(1, voice=True))
    assert heard == [(b"OggS-fake-voice", "file_7.oga")], heard
    assert fake.sent() == ['🎤 I heard: "play tum hi ho by arijit singh"',
                           "answer to 'play tum hi ho by arijit singh'"], fake.sent()


def test_one_chat_is_one_conversation_and_new_resets():
    threads_seen = []

    async def turn(text, thread):
        threads_seen.append(thread)
        return "ok", thread or f"thread-{len(threads_seen)}"
    fake = FakeTelegram()
    bot = make_bot(fake, turn=turn)
    run(bot, message(1, "suggest a comedy"), message(2, "play the first one"))
    run(bot, message(3, "/new"), message(4, "is the tv on?"))
    assert threads_seen == [None, "thread-1", None], threads_seen
    assert "Started a fresh conversation." in fake.sent()


def test_the_offset_moves_on():
    fake = FakeTelegram()
    bot = make_bot(fake)
    run(bot, message(5, "a"), message(6, "b"))
    run(bot)                                                         # nothing new
    offsets = [b.get("offset") for m, b in fake.calls if m == "getUpdates"]
    assert offsets == [0, 7], offsets
    assert len(fake.sent()) == 2


def test_groups_are_ignored():
    fake = FakeTelegram()
    bot = make_bot(fake)
    run(bot, message(1, "play x", chat_type="group"))
    assert fake.sent() == []


def test_a_login_problem_is_explained():
    async def turn(text, thread):
        raise login.LoginError("Nobody is logged in.")
    fake = FakeTelegram()
    bot = make_bot(fake, turn=turn)
    run(bot, message(1, "play x"))
    assert fake.sent() == ["Login problem on the PC: Nobody is logged in.  (run: python -m common.login)"]


def test_end_to_end_a_question_and_its_answer():
    # The REAL graph.chat_turn: fake catalog + fake TV (dry run), no model, checkpoints in a temporary folder.
    saved = config.STATE_DIR, agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL
    config.STATE_DIR = Path(tempfile.mkdtemp())
    agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL = NoModel, dict(FIXED), "", ""
    try:
        fake = FakeTelegram()
        http = httpx.AsyncClient(transport=fake.transport)
        bot = telegram_bot.TelegramBot(TOKEN, [str(ME)], http, threads_file=config.STATE_DIR / "threads.json")
        bot.fake = fake
        run(bot, message(1, "play Dual Title 2020"))
        run(bot, message(2, "prime"))
    finally:
        config.STATE_DIR, agent.AsyncGroq, agent.SERVERS, config.ROUTER_MODEL, config.CATALOG_AGENT_URL = saved
    asked, played = fake.sent()
    assert "Which one would you like?" in asked, asked
    assert "Prime Video" in played and "simulation" in played, played


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
