"""
Step-by-step probe of the Netflix profile switch (Phase 2, Step 9 debugging).

Run it with the TV on and WATCH THE SCREEN. It does one step at a time and asks
you what you see, so we learn which step fails: closing Netflix, the picker
timing, or the key presses. It never plays anything.

    python -m tools.profile_probe            # tries position 4 (Mishika)
    python -m tools.profile_probe 3          # another position
"""
import asyncio
import sys
import time

from common import config
from servers.device import tv

APP = config.NETFLIX_APP


async def main(position: int):
    c = await tv.connect()
    try:
        print(f"1. Connected. Foreground app now: {c.tv_state.current_app_id}")

        await tv.ask("\n2. Press Enter to CLOSE Netflix (watch the TV)... ")
        try:
            resp = await c.close_app(APP)
            print(f"   close_app answered: {resp}")
        except Exception as e:  # noqa: BLE001
            print(f"   close_app FAILED: {type(e).__name__}: {e}")
        await asyncio.sleep(3)
        print(f"   Foreground app 3 s later: {c.tv_state.current_app_id}")
        await tv.ask("   Did Netflix disappear from the screen? Note it, then press Enter... ")

        await tv.ask("\n3. Press Enter to LAUNCH Netflix... ")
        start = time.monotonic()
        await c.launch_app(APP)
        await tv.ask("   Press Enter the moment the profile picker ('Who's watching?') is fully shown... ")
        print(f"   Picker took {time.monotonic() - start:.0f} s (the code waits NETFLIX_PICKER_WAIT="
              f"{config.NETFLIX_PICKER_WAIT} s). If no picker appeared, say what is on screen.")

        keys = tv.profile_keys(position, config.NETFLIX_PICKER_LAYOUT)[:-1]   # all but ENTER
        reset, move = keys[:config.PROFILE_RESET_PRESSES], keys[config.PROFILE_RESET_PRESSES:]
        await tv.ask(f"\n4. Press Enter to send {reset[0]} x {len(reset)}... ")
        await tv.press(c, reset, gap=0.8)
        await tv.ask("   Is the FIRST (top) profile highlighted now, and NOT in edit mode? Press Enter... ")

        await tv.ask(f"\n5. Press Enter to send {move[0] if move else '(nothing)'} x {len(move)}... ")
        await tv.press(c, move, gap=0.8)
        await tv.ask(f"   Is profile number {position} highlighted? Note it, then press Enter... ")

        answer = await tv.ask("\n6. Send ENTER to pick it? (y/n) ")
        if answer.strip().lower().startswith("y"):
            await c.button("ENTER")
            print("   Sent. Which profile's home screen opened?")
    finally:
        await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 4))