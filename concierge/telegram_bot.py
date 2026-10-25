"""
A Telegram bot as a FRONT END to the concierge: type, or send a voice note, from any phone.

    phone (Telegram app) --> Telegram --> THIS program (on the PC) --> graph.chat_turn --> agents --> the TV
                         <-- the answer, as a chat message <--

HOW IT TALKS TO TELEGRAM (the Bot API over HTTPS, nothing else installed)
    LONG POLLING: we ASK Telegram "any new messages?" (getUpdates, waiting up to 50 s for one). Every connection goes
    OUT from this PC: no open port, no HTTPS certificate, no Tailscale needed. `offset` tells Telegram which messages
    we have already handled, so a message is never handled twice (and never lost if we restart).

WHAT IT DOES WITH A MESSAGE
    1. WHO: only TELEGRAM_ALLOWED_IDS (numeric Telegram user ids). Anyone else who finds the bot is IGNORED: no reply
       at all (their id is printed here once, so YOU can allow yourself on the first try). Private chats only.
    2. VOICE: a voice note is downloaded and turned into text by Groq's speech-to-text (TRANSCRIBE_MODEL); the bot
       replies "I heard: ..." so you see what it understood. Then it is handled exactly like typed text.
    3. THE CONVERSATION: each Telegram chat keeps ONE conversation thread (idea 1: questions and answers, history,
       "play the first one"), saved in .state/telegram_threads.json, so it survives a restart. /new starts a fresh one.
    4. graph.chat_turn(text, thread) -> the answer -> sendMessage. While it works, Telegram shows "typing...".
    Identity (until C2, per-person logins): the agents act as whoever is logged in on THIS PC (.login.json).

Run:   python -m concierge.telegram_bot       (needs TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_IDS in .env)
"""
import asyncio
import json
import sys

import httpx

from common import config
from common import login
from common import tracing
from concierge import engine
from concierge import graph

API = "https://api.telegram.org"
POLL_SECONDS = 50                 # Telegram holds the request open up to this long when there is nothing new
MAX_MESSAGE = 4000                # Telegram's limit is 4096 characters per message
HELP = ("Hi! Tell me what to watch, by text or a voice note. For example:\n"
        "• play the diplomat\n• play tum hi ho by arijit singh\n• is the tv on?\n"
        "• suggest a comedy for me, then: play the first one\n"
        "When I ask a question (Netflix or Prime?), just answer it. /new starts a fresh conversation.")


async def groq_transcribe(audio: bytes, filename: str) -> str:
    """Speech to text with Groq (the same key as the agents). Telegram voice notes are OGG/Opus: Groq accepts them."""
    result = await engine.AsyncGroq().audio.transcriptions.create(model=config.TRANSCRIBE_MODEL, file=(filename, audio))
    return (getattr(result, "text", "") or "").strip()


class TelegramBot:
    def __init__(self, token: str, allowed_ids, http: httpx.AsyncClient, turn=None, transcribe=None,
                 threads_file=None):
        self.token, self.http = token, http
        self.allowed = {str(i) for i in allowed_ids}
        self.turn = turn or graph.chat_turn                  # (text, thread) -> (answer, thread)
        self.transcribe = transcribe or groq_transcribe
        self.threads_file = threads_file or (config.STATE_DIR / "telegram_threads.json")
        self.threads = self._load_threads()                  # chat id -> conversation thread id
        self.offset = 0
        self.ignored: set[str] = set()

    # ---------------------------------------------------------------- the Bot API
    async def call(self, method: str, **params):
        r = await self.http.post(f"{API}/bot{self.token}/{method}", json=params,
                                 timeout=POLL_SECONDS + 15 if method == "getUpdates" else 30)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', r.status_code)}")
        return data["result"]

    async def download(self, file_id: str) -> tuple[bytes, str]:
        info = await self.call("getFile", file_id=file_id)
        r = await self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}", timeout=60)
        r.raise_for_status()
        return r.content, info["file_path"].rsplit("/", 1)[-1]

    async def say(self, chat_id, text: str):
        text = text or "(no answer)"
        for i in range(0, len(text), MAX_MESSAGE):
            await self.call("sendMessage", chat_id=chat_id, text=text[i:i + MAX_MESSAGE])

    # ---------------------------------------------------------------- the loop
    async def poll_once(self) -> int:
        """Fetch new messages (waiting up to POLL_SECONDS) and handle them in order. Returns how many."""
        updates = await self.call("getUpdates", offset=self.offset, timeout=POLL_SECONDS,
                                  allowed_updates=["message"])
        for update in updates:
            self.offset = update["update_id"] + 1          # handled (or skipped): never fetched again
            await self.handle(update)
        return len(updates)

    async def handle(self, update: dict):
        msg = update.get("message") or {}
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        user_id = str(sender.get("id", ""))
        if chat.get("type") != "private":
            return                                          # groups and channels: not for this bot
        if user_id not in self.allowed:
            if user_id not in self.ignored:                 # no reply to strangers; tell the OWNER, here, once
                self.ignored.add(user_id)
                print(f"[telegram] IGNORED a message from id {user_id} (@{sender.get('username', '?')}). "
                      f"To allow it: TELEGRAM_ALLOWED_IDS={user_id} in .env, then restart the bot.", flush=True)
            return
        chat_id = chat["id"]
        text = (msg.get("text") or "").strip()
        if "voice" in msg or "audio" in msg:
            text = await self.hear(chat_id, (msg.get("voice") or msg.get("audio"))["file_id"])
            if not text:
                return
        if not text:
            await self.say(chat_id, "I can read text and voice notes.")
            return
        if text in ("/start", "/help"):
            await self.say(chat_id, HELP)
            return
        if text == "/new":
            self.threads.pop(str(chat_id), None)
            self._save_threads()
            await self.say(chat_id, "Started a fresh conversation.")
            return
        print(f"[telegram] {sender.get('username', user_id)}: {text[:100]!r}", flush=True)
        await self.say(chat_id, await self.answer(chat_id, text))

    async def hear(self, chat_id, file_id: str) -> str:
        try:
            audio, name = await self.download(file_id)
            text = await self.transcribe(audio, name)
        except Exception as e:  # noqa: BLE001
            await self.say(chat_id, f"I couldn't turn that voice note into text ({type(e).__name__}). Try typing it.")
            return ""
        await self.say(chat_id, f'🎤 I heard: "{text}"' if text else "I couldn't hear any words in that.")
        return text

    async def answer(self, chat_id, text: str) -> str:
        """One turn of this chat's conversation, with "typing..." shown while the agents work."""
        typing = asyncio.create_task(self._keep_typing(chat_id))
        try:
            answer, thread = await self.turn(text, self.threads.get(str(chat_id)))
            self.threads[str(chat_id)] = thread
            self._save_threads()
            return answer
        except Exception as e:  # noqa: BLE001   the bot must keep running; the person must hear WHY
            cause = engine.first_cause(e)
            if isinstance(cause, login.LoginError):
                return f"Login problem on the PC: {cause}  (run: python -m common.login)"
            return f"Sorry, that failed: {type(cause).__name__}: {cause}"
        finally:
            typing.cancel()

    async def _keep_typing(self, chat_id):
        while True:                                         # Telegram shows "typing..." for ~5 s per call
            try:
                await self.call("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:  # noqa: BLE001   cosmetic only
                pass
            await asyncio.sleep(4)

    # ---------------------------------------------------------------- which chat is which conversation
    def _load_threads(self) -> dict:
        try:
            return json.loads(self.threads_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_threads(self):
        self.threads_file.parent.mkdir(parents=True, exist_ok=True)
        self.threads_file.write_text(json.dumps(self.threads), encoding="utf-8")


async def run_forever(bot: TelegramBot):
    failures = 0
    while True:
        try:
            await bot.poll_once()
            failures = 0
        except Exception as e:  # noqa: BLE001   network blips: wait and try again, longer each time (max 60 s)
            failures += 1
            wait = min(60, 2 ** failures)
            print(f"[telegram] polling failed ({type(e).__name__}); retrying in {wait}s", flush=True)
            await asyncio.sleep(wait)


async def main_async():
    async with httpx.AsyncClient() as http:
        bot = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_ALLOWED_IDS, http)
        me = await bot.call("getMe")
        print(f"Telegram bot @{me['username']} is running (long polling: no open port). Ctrl+C to stop.", flush=True)
        if not bot.allowed:
            print("Nobody is allowed yet: send the bot a message from your phone; your id will be printed here.",
                  flush=True)
        await run_forever(bot)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    config.require("TELEGRAM_BOT_TOKEN")
    config.require("GROQ_API_KEY")
    tracing.setup("watch-concierge (telegram)")
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("stopped")
    finally:
        tracing.finish()


if __name__ == "__main__":
    main()
