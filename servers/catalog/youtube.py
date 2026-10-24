"""
YouTube SEARCH for catalog-mcp (search_youtube). Searched by name with yt-dlp, not by a streaming-catalogue
id. Playing a video is device-mcp's job (servers/device/services/youtube.py).

If it breaks after a YouTube change: pip install -U yt-dlp
"""


def find_videos(query: str, limit: int = 3) -> list[dict]:
    """Top `limit` YouTube results as dicts: video_id, title, channel, duration_seconds.
    Blocking (yt-dlp): call it with asyncio.to_thread."""
    from yt_dlp import YoutubeDL
    opts = {"quiet": True, "skip_download": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    return [{"video_id": e["id"], "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
             "duration_seconds": e.get("duration")} for e in (info.get("entries") or []) if e.get("id")]
