"""
Per-person TV profiles (Phase 2, Step 9), tested WITHOUT a TV.

A fake TV records every command, so we can check the exact key presses for
each person. Waits are skipped. Run:  python tests/test_profiles.py  (or: pytest tests)
"""
import asyncio
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config
from servers.device import tv
from servers.device.services import netflix, prime


class FakeTV:
    def __init__(self, foreground="com.webos.app.home"):
        self.log = []
        self.tv_state = type("S", (), {"current_app_id": foreground})()

    async def close_app(self, app):
        self.log.append(f"close {app}")

    async def launch_app(self, app):
        self.log.append(f"launch {app}")

    async def launch_app_with_content_id(self, app, content_id):
        self.log.append(f"link {app}")

    async def button(self, key):
        self.log.append(key)


async def _no_wait(*_args, **_kwargs):
    return None


def run_flow(flow, foreground="com.webos.app.home", **kwargs):
    fake = FakeTV(foreground)
    real_sleep = asyncio.sleep
    asyncio.sleep = _no_wait          # skip the real waits (seconds each)
    try:
        with contextlib.redirect_stdout(io.StringIO()):   # tv.py prints each key; keep the report clean
            asyncio.run(flow(fake, **kwargs))
    finally:
        asyncio.sleep = real_sleep
    return fake.log


def keys(log):
    return [entry for entry in log if entry.isupper()]


# ----------------------------------------------------------------- tests
def test_profile_keys_go_to_the_first_profile_first():
    # The picker highlights the LAST USED profile, so we cannot count from it.
    n = config.PROFILE_RESET_PRESSES
    assert tv.profile_keys(1, "row") == ["LEFT"] * n + ["ENTER"]
    assert tv.profile_keys(4, "column") == ["UP"] * n + ["DOWN"] * 3 + ["ENTER"]


def test_pickers_never_get_left_or_right():
    # Verified live: both pickers stack profiles; on Netflix LEFT/RIGHT reach the edit pencil.
    for layout in (config.NETFLIX_PICKER_LAYOUT, config.PRIME_PICKER_LAYOUT):
        assert layout == "column"
        for position in (1, 3, 4):
            assert not {"LEFT", "RIGHT"} & set(tv.profile_keys(position, layout))


def test_household_positions():
    assert config.NETFLIX_PROFILES == {"gaurav": 3, "mishika": 4}
    assert config.PRIME_PROFILES == {"gaurav": 1, "mishika": 3}


def test_netflix_restarts_and_picks_mishika_even_if_netflix_is_open():
    log = run_flow(lambda c: netflix.open_title(c, "82182295", True, profile=4),
                   foreground=config.NETFLIX_APP)
    # close first: an already-open Netflix would keep the previous person's profile
    assert log[:2] == [f"close {config.NETFLIX_APP}", f"launch {config.NETFLIX_APP}"], log
    assert keys(log) == tv.profile_keys(4, config.NETFLIX_PICKER_LAYOUT) + ["ENTER"], log        # pick profile, then Play
    assert log.index(f"link {config.NETFLIX_APP}") > log.index("DOWN"), "link must come after the profile"


def test_prime_picks_gaurav_while_the_picker_holds_the_link():
    log = run_flow(lambda c: prime.open_title(c, "amzn1.dv.gti.0000-1111", False, profile=1))
    first_key = tv.profile_keys(1, config.PRIME_PICKER_LAYOUT)[0]
    assert log.index(f"link {config.PRIME_APP}") < log.index(first_key), "Prime needs the link BEFORE the profile"
    assert keys(log) == tv.profile_keys(1, config.PRIME_PICKER_LAYOUT), log


def test_unknown_person_keeps_the_old_behaviour():
    log = run_flow(lambda c: netflix.open_title(c, "82182295", True))     # profile=None
    assert "close" not in " ".join(log) and keys(log) == ["ENTER", "ENTER"], log


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