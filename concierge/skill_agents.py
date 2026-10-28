"""
The agents built from SKILL FILES (concierge/skills/*.md), plus the small code helpers they need.

    taste_agent()        skills/taste.md: the logged-in person's likes/dislikes; only taste-mcp; NO household
                         permission (get_household_taste is hidden from it)
    consensus_agent()    skills/suggest.md: suggestions for one person or several; taste-mcp WITH the extra
                         scope taste-household-read; titles confirmed by check_titles (code)
    check_titles         a CODE tool: up to 12 search_title calls (4 at a time); only confirmed titles come back
    has_taste()          is taste-mcp configured (i.e. full mode, with a login)?
    logged_in_username() who is logged in, from the login state (code, never a model)

Used by the graph's taste / suggest nodes, and by the team's bridges (concierge.team).
"""
import asyncio
import json

from common import login
from concierge import engine, skills

# Phase 3, Step 7: the Taste and Consensus prompts live in skill files (concierge/skills/*.md), together with
# their tools, permissions and turn budget. The graph and the team share them.
TASTE_SKILL = skills.load("taste")
SUGGEST_SKILL = skills.load("suggest")
CONSENSUS_SYSTEM = SUGGEST_SKILL.instructions    # kept as a name: test_household reads it


def taste_agent() -> engine.Agent:
    """A specialist built from skills/taste.md: only taste-mcp, NO household permission;
    get_household_taste is hidden from it (and taste-mcp would refuse its token anyway)."""
    return skills.build_agent(TASTE_SKILL, "taste", engine.SERVERS, {})


def consensus_agent(trace: list | None = None) -> engine.Agent:
    """A specialist built from skills/suggest.md. It needs MORE permission than the others, so it alone asks
    for it: a taste-mcp token WITH taste-household-read, and only get_household_taste offered. Titles are
    checked by check_titles (CODE, in parallel), not by nested Catalog agents (Step 7: 2 model calls per
    title saved, and ids no longer copied through two models)."""
    return skills.build_agent(SUGGEST_SKILL, "consensus", engine.SERVERS,
                              {"check_titles": check_titles_tool(trace)})


MAX_TITLES_CHECKED = 12     # was 4 (Phase 3): "give me 10 comedies" got 3-4. Each title = one catalog search.
PARALLEL_SEARCHES = 4       # at most this many searches at once: 12 at once could trip the API's rate limit


def check_titles_tool(trace: list | None) -> engine.LocalTool:
    """A CODE tool: look up several titles at once (in parallel) and return only what the catalog confirms.
    It opens its own catalog-mcp connection (token: catalog-mcp only). No model is involved."""

    async def check(args: dict) -> str:
        wanted = []
        for t in (args.get("titles") or [])[:MAX_TITLES_CHECKED]:
            if isinstance(t, str):
                t = {"title": t}
            title = " ".join(str(t.get("title") or "").split())
            if title:
                wanted.append({"title": title, **({"year": str(t["year"])} if t.get("year") else {})})
        if not wanted:
            raise ValueError(f"titles is empty: give up to {MAX_TITLES_CHECKED} titles, "
                             "e.g. [{\"title\": \"Inception\", \"year\": \"2010\"}]")
        gate = asyncio.Semaphore(PARALLEL_SEARCHES)

        async def search(box, w):
            async with gate:
                return await engine.call_traced(box, "search_title", w, trace, "check_titles")

        async with engine.McpToolbox({"catalog": engine.SERVERS["catalog"]}) as box:
            results = await asyncio.gather(*(search(box, w) for w in wanted))
        confirmed, not_found, errors = [], [], []
        for w, (text, is_error) in zip(wanted, results):
            if is_error:
                errors.append({"title": w["title"], "error": " ".join(text.split())[:200]})
                continue
            matches = json.loads(text).get("matches", [])
            (confirmed.extend(matches) if matches else not_found.append(w["title"]))
        return json.dumps({"confirmed": confirmed, "not_found": not_found, "errors": errors})

    return engine.LocalTool(
        name="check_titles",
        description=(f"Check up to {MAX_TITLES_CHECKED} titles at once in the catalog (Netflix and Prime in India). Returns the "
                     "confirmed ones with year, service and content id, and the ones not found."),
        parameters={"type": "object", "properties": {"titles": {
            "type": "array", "description": "e.g. [{\"title\": \"Inception\", \"year\": \"2010\"}]",
            "items": {"type": "object", "properties": {"title": {"type": "string"}, "year": {"type": "string"}},
                      "required": ["title"]}}}, "required": ["titles"]},
        fn=check, owner="code (catalog check)", read_only=True)


def has_taste() -> bool:
    return "taste" in engine.SERVERS          # taste-mcp is HTTP only (needs a login)


def logged_in_username() -> str | None:
    """Who is logged in, from login.py's state (CODE, never a model). None without a login."""
    try:
        state = login.load_login()
    except Exception:  # noqa: BLE001   a broken state file just means "unknown"
        return None
    return (state or {}).get("username")