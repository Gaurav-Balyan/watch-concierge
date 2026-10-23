"""
Title lookup: turn a title name into playable IDs per streaming service (India).

Only parses API data. It never touches the TV, so it can be tested offline
with a saved JSON response.

PHASE 4: the call to the outside API is wrapped, outermost first:
    cache (fresh, CATALOG_CACHE_TTL)  ->  circuit breaker  ->  retry with backoff  ->  HTTP
    and if the API still fails: an OLDER cached answer (stale-if-error, up to CATALOG_STALE_MAX).
Why this order: a cache hit costs nothing and needs no API at all; the breaker fails FAST when the API
has been failing (no point retrying into an outage); retries only absorb short, TRANSIENT problems.
State (cache + breaker) is .state/catalog.sqlite, shared by every catalog-mcp process.
"""
import re
import sys
from dataclasses import dataclass, field

import httpx
from opentelemetry import trace as otel

import config
import resilience
import tracing


@dataclass
class Match:
    title: str
    year: str
    ids: dict = field(default_factory=dict)   # e.g. {"netflix": "82182295", "prime": "amzn1.dv.gti.<uuid>"}


_stores: tuple | None = None


def stores() -> tuple[resilience.TtlCache, resilience.CircuitBreaker]:
    """Opened on first use (tests point config.STATE_DIR elsewhere first)."""
    global _stores
    if _stores is None:
        path = config.STATE_DIR / "catalog.sqlite"
        _stores = (resilience.TtlCache(path),
                   resilience.CircuitBreaker("catalog API", path, config.CATALOG_BREAKER_FAILURES,
                                             config.CATALOG_BREAKER_RESET))
    return _stores


def transient(e: Exception) -> bool:
    """Worth retrying: it can go away by itself in a second or two."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 429:              # a SHORT rate limit only: RapidAPI quotas are monthly
            wait = e.response.headers.get("Retry-After", "")
            return wait.isdigit() and int(wait) <= 5
        return status >= 500
    return isinstance(e, httpx.TransportError)       # timeouts, refused / dropped connections


def _note(text: str):
    print(f"[catalog] {text}", file=sys.stderr)      # stderr: stdout is the MCP channel


async def search_shows(title: str) -> list:
    """The raw show list for `title` in India: from the cache, or the API (with retries + breaker)."""
    key = " ".join(title.lower().split())
    cache, breaker = stores()
    span = otel.get_current_span()
    hit = cache.get(key, config.CATALOG_CACHE_TTL)
    if hit is not None:
        tracing.set_attrs(span, {"catalog.source": "cache", "catalog.cache_age_s": round(hit[1])})
        return hit[0]

    attempts = [0]

    async def once():
        attempts[0] += 1
        return await _api_search(title)

    try:
        shows = await breaker.call(lambda: resilience.retry_async(
            once, attempts=config.CATALOG_RETRIES, base=config.CATALOG_BACKOFF, retry_if=transient,
            on_retry=lambda n, e, d: _note(f"attempt {n} failed ({type(e).__name__}); retrying in {d:.1f}s")))
    except (resilience.CircuitOpen, httpx.HTTPError) as e:
        stale = cache.get(key, config.CATALOG_STALE_MAX)
        tracing.set_attrs(span, {"catalog.attempts": attempts[0], "catalog.error": type(e).__name__})
        if stale is not None:
            _note(f"API failed ({type(e).__name__}); answering from a cached copy {stale[1] / 3600:.0f} h old")
            tracing.set_attrs(span, {"catalog.source": "stale cache", "catalog.cache_age_s": round(stale[1])})
            return stale[0]
        raise
    tracing.set_attrs(span, {"catalog.source": "api", "catalog.attempts": attempts[0]})
    cache.put(key, shows)
    return shows


async def _api_search(title: str) -> list:
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