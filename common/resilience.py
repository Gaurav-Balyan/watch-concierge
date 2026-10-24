"""
Phase 4: the resilience patterns, small and in one place.

    retry_async      try again after a TRANSIENT failure, waiting longer each time (backoff) and a
                     random part of it (jitter, so many clients do not retry in lock-step).
                     Only for errors that can go away (timeouts, 5xx, a short 429). Never for a bad key.
    CircuitBreaker   after N failures IN A ROW, stop calling the service for a while ("open"): callers
                     fail FAST instead of each waiting through its own retries. After the pause, ONE call
                     is let through ("half-open"): success closes it, failure opens it again.
    TtlCache         remember answers for a while. When the service is down, an older answer
                     (stale-if-error) is better than none.
    IdempotencyStore "this request already did this action": record the INTENT before acting, the
                     RESULT after. A repeat of the same key gets the first result back, never a second
                     action. A crash mid-action leaves "started": the outcome is unknown, so do not repeat.

All state is a small SQLite file (config.STATE_DIR), not memory: in quick mode every request starts
fresh server processes, and after a crash memory is gone. That is the point of these patterns.
"""
import asyncio
import json
import random
import sqlite3
import time
from contextlib import closing
from pathlib import Path


# ---------------------------------------------------------------- retry with backoff + jitter

async def retry_async(fn, *, attempts: int, base: float, max_delay: float = 8.0, retry_if=lambda e: True,
                      on_retry=None, sleep=asyncio.sleep):
    """Call `await fn()`, retrying when `retry_if(error)` is True. Waits base, 2*base, 4*base ... (capped),
    each scaled by a random 50-100% ("equal jitter"). The last error is raised as it was."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001   decided by retry_if
            if attempt == attempts or not retry_if(e):
                raise
            delay = min(max_delay, base * 2 ** (attempt - 1))
            delay = delay / 2 + random.uniform(0, delay / 2)
            if on_retry:
                on_retry(attempt, e, delay)
            await sleep(delay)


# ---------------------------------------------------------------- small SQLite helper

def _db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10, isolation_level=None)   # autocommit; one statement each
    con.execute("PRAGMA journal_mode=WAL")                                # several processes may share it
    return con


# ---------------------------------------------------------------- circuit breaker

class CircuitOpen(Exception):
    def __init__(self, name: str, retry_in: float):
        super().__init__(f"{name} is temporarily switched off after repeated failures (circuit open); "
                         f"next try in {retry_in:.0f}s")
        self.name, self.retry_in = name, retry_in


class CircuitBreaker:
    def __init__(self, name: str, path: Path, failures: int, reset_after: float, clock=time.time):
        self.name, self.path, self.threshold, self.reset_after, self.clock = name, path, failures, reset_after, clock
        with closing(_db(path)) as con:
            con.execute("CREATE TABLE IF NOT EXISTS breaker (name TEXT PRIMARY KEY, failures INTEGER, "
                        "opened_at REAL)")

    def state(self) -> tuple[str, float]:
        """('closed' | 'open' | 'half_open', seconds until the next try when open)."""
        with closing(_db(self.path)) as con:
            row = con.execute("SELECT failures, opened_at FROM breaker WHERE name=?", (self.name,)).fetchone()
        if not row or row[1] is None:
            return "closed", 0.0
        left = row[1] + self.reset_after - self.clock()
        return ("open", left) if left > 0 else ("half_open", 0.0)

    async def call(self, fn):
        state, left = self.state()
        if state == "open":
            raise CircuitOpen(self.name, left)            # fail FAST: no call at all
        try:
            result = await fn()
        except Exception:
            self._failed()            # half-open + a failure: the count is still >= threshold -> open again
            raise
        self._reset()
        return result

    def _failed(self):
        with closing(_db(self.path)) as con:
            row = con.execute("SELECT failures FROM breaker WHERE name=?", (self.name,)).fetchone()
            n = (row[0] if row else 0) + 1
            opened = self.clock() if n >= self.threshold else None
            con.execute("INSERT OR REPLACE INTO breaker VALUES (?, ?, ?)", (self.name, n, opened))

    def _reset(self):
        with closing(_db(self.path)) as con:
            con.execute("DELETE FROM breaker WHERE name=?", (self.name,))


# ---------------------------------------------------------------- cache

class TtlCache:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        with closing(_db(path)) as con:
            con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, stored_at REAL)")

    def get(self, key: str, max_age: float):
        """(value, age_seconds) if stored less than max_age ago, else None."""
        with closing(_db(self.path)) as con:
            row = con.execute("SELECT value, stored_at FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        age = self.clock() - row[1]
        return (json.loads(row[0]), age) if age < max_age else None

    def put(self, key: str, value):
        with closing(_db(self.path)) as con:
            con.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, json.dumps(value), self.clock()))


# ---------------------------------------------------------------- idempotency

class IdempotencyStore:
    KEEP = 7 * 24 * 3600          # records older than a week are dropped

    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        with closing(_db(path)) as con:
            con.execute("CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, what TEXT, status TEXT, "
                        "result TEXT, at REAL)")
            con.execute("DELETE FROM actions WHERE at < ?", (clock() - self.KEEP,))

    def begin(self, key: str, what: dict) -> tuple[str, dict | None, dict | None]:
        """Claim `key` for the action `what` BEFORE doing it. Returns (status, earlier_what, earlier_result):
            ("new", None, None)       claimed: go ahead
            ("done", what, result)    already done: return that result, do NOT act again
            ("started", what, None)   an earlier attempt started and never finished: outcome UNKNOWN
        The INSERT is atomic: two attempts racing for one key cannot both get "new"."""
        with closing(_db(self.path)) as con:
            try:
                con.execute("INSERT INTO actions VALUES (?, ?, 'started', NULL, ?)",
                            (key, json.dumps(what, sort_keys=True), self.clock()))
                return "new", None, None
            except sqlite3.IntegrityError:
                w, status, result = con.execute("SELECT what, status, result FROM actions WHERE key=?",
                                                (key,)).fetchone()
        return status, json.loads(w), (json.loads(result) if result else None)

    def finish(self, key: str, result: dict):
        with closing(_db(self.path)) as con:
            con.execute("UPDATE actions SET status='done', result=? WHERE key=?", (json.dumps(result), key))

    def forget(self, key: str):
        """The action certainly did NOT happen (e.g. the TV never answered): a retry may do it."""
        with closing(_db(self.path)) as con:
            con.execute("DELETE FROM actions WHERE key=?", (key,))
