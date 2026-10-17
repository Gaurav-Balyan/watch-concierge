"""
Title lookup: turn a title name into playable IDs per streaming service (India).

Only parses API data. It never touches the TV, so it can be tested offline
with a saved JSON response.
"""
import re
from dataclasses import dataclass, field

import httpx

import config


@dataclass
class Match:
    title: str
    year: str
    ids: dict = field(default_factory=dict)   # e.g. {"netflix": "82182295", "prime": "amzn1.dv.gti.<uuid>"}


async def search_shows(title: str) -> list:
    """One Streaming Availability search for India. Returns the raw show list."""
    rapid = config.require("RAPIDAPI_KEY")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(
            "https://streaming-availability.p.rapidapi.com/shows/search/title",
            params={"title": title, "country": config.COUNTRY},
            headers={
                "X-RapidAPI-Key": rapid,
                "X-RapidAPI-Host": "streaming-availability.p.rapidapi.com",
            },
        )
    r.raise_for_status()
    return r.json()


def show_year(show: dict) -> str:
    """Movies have releaseYear; series have firstAirYear. Use whichever exists."""
    return str(show.get("releaseYear") or show.get("firstAirYear") or "?")


def extract_ids(show: dict) -> dict:
    """Pull playable IDs for the services we can control.

    Prime only counts when it is a subscription option (not rent/buy), so the
    script never lands on a payment screen.
    """
    ids = {}
    for o in show.get("streamingOptions", {}).get(config.COUNTRY, []):
        service = o.get("service", {}).get("id")
        link = o.get("link", "") or ""
        if service == "netflix" and "netflix" not in ids:
            m = re.search(r"/title/(\d+)", link)
            if m:
                ids["netflix"] = m.group(1)
        elif service == "prime" and o.get("type") == "subscription" and "prime" not in ids:
            m = re.search(r"gti=(amzn1\.dv\.gti\.[0-9a-fA-F-]+)", link)
            if m:
                ids["prime"] = m.group(1)
    return ids


def matches_from_shows(shows: list, year: str | None = None, only: str | None = None) -> list[Match]:
    """Filter raw shows down to those playable on a supported service."""
    found = []
    for show in shows:
        ids = extract_ids(show)
        if only:
            ids = {k: v for k, v in ids.items() if k == only}
        if ids:
            found.append(Match(show.get("title", "?"), show_year(show), ids))
    if year:
        found = [m for m in found if m.year == str(year)]
    return found


async def find_title(title: str, year: str | None = None, only: str | None = None) -> list[Match]:
    """Search and return playable matches. `only` restricts to one service."""
    return matches_from_shows(await search_shows(title), year, only)


def pick_service(ids: dict, only: str | None = None) -> str | None:
    """Choose a service for a match, following SERVICE_PREFERENCE."""
    if only:
        return only if only in ids else None
    for service in config.SERVICE_PREFERENCE:
        if service in ids:
            return service
    return next(iter(ids), None)
