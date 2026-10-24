"""
The fake catalog used by the evals.

Fixed data means a case never depends on RapidAPI, never uses your quota, and
gives the same answer every run. The ids below are what the checks compare against.
"""
from servers.catalog.lookup import Match

THE_FRIEND_NETFLIX = "82182295"
GAME_CHANGER_2025_PRIME = "amzn1.dv.gti.5dc381a7-270a-4b0b-b5d1-e7a50f47345f"
GAME_CHANGER_2021_NETFLIX = "81048780"
DUAL_NETFLIX = "70000001"
DUAL_PRIME = "amzn1.dv.gti.11111111-2222-3333-4444-555555555555"

CATALOG = [
    ("The Friend", "2025", {"netflix": THE_FRIEND_NETFLIX}),
    ("Game Changer", "2025", {"prime": GAME_CHANGER_2025_PRIME}),
    ("Game Changer", "2021", {"netflix": GAME_CHANGER_2021_NETFLIX}),
    ("Dual Title", "2020", {"netflix": DUAL_NETFLIX, "prime": DUAL_PRIME}),
]


YOUTUBE_TUM_HI_HO = "Umqb9KENgmk"
YOUTUBE = [  # fixed YouTube results for the evals and tests (no network)
    ("tum hi ho", [{"video_id": YOUTUBE_TUM_HI_HO, "title": "Tum Hi Ho - Aashiqui 2 | Arijit Singh",
                    "channel": "T-Series", "duration_seconds": 262}]),
]


def find_videos(query: str, limit: int = 3) -> list[dict]:
    """Same signature as services.youtube.find_videos, over the fixed list."""
    q = query.lower()
    for key, videos in YOUTUBE:
        if key in q:
            return videos[:limit]
    return []


async def search(title: str, year: str | None = None, only: str | None = None) -> list[Match]:
    """Same signature and result type as lookup.find_title, but over the fixed catalog."""
    q = " ".join(title.lower().split())
    found = []
    for t, y, ids in CATALOG:
        if q and q in t.lower():
            ids = {k: v for k, v in ids.items() if not only or k == only}
            if ids:
                found.append(Match(t, y, dict(ids)))
    if year:
        found = [m for m in found if m.year == str(year)]
    return found