"""
The RECORDER: after a real play, CODE (never a model) writes it to the logged-in person's watch history.
Used by the PLAY / SONG workflows and by the team's Supervisor (its after_tool hook).
"""
import json

from common import tracing
from concierge import engine, skill_agents


async def record_watch_after_play(_supervisor_box, name: str, args: dict, result_text: str):
    """The Supervisor's after_tool hook. The Supervisor has no taste tools, so after a real play
    this CODE opens its own short taste-mcp connection (token: taste-mcp only) to record it."""
    if name != "play_title" or not skill_agents.has_taste():
        return
    try:
        result = json.loads(result_text)
        if result.get("status") != "requested" or result.get("duplicate"):
            return                    # dry run, failure, or a repeat (Phase 4: already recorded): no connection
    except json.JSONDecodeError:
        return
    with tracing.span("recorder record_watch", {"recorder.reason": "real play (status requested)",
                                                "tool.args": {"title": args.get("title")}}):
        async with engine.McpToolbox({"taste": engine.SERVERS["taste"]}) as taste_box:
            await engine.record_watch_if_played(taste_box, args, result_text)
