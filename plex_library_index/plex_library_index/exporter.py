"""Plex library exporter — importable module.

Synchronous function `run_export(opts, log_fn)` connects to a Plex server,
walks every movie and TV section, and returns a payload dict ready for JSON
encoding. No I/O beyond the Plex API calls.
"""
import concurrent.futures
import re
from datetime import datetime, timezone

from plexapi.server import PlexServer

GUID_PATTERNS = {
    "imdb": re.compile(r"imdb://(tt\d+)"),
    "tmdb": re.compile(r"tmdb://(\d+)"),
    "tvdb": re.compile(r"tvdb://(\d+)"),
}


def _iso_or_none(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return str(dt)


def _resolution_bucket(height):
    if not height:
        return None
    if height >= 2000:
        return "4K"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    return "SD"


def _media_summary(item):
    media = getattr(item, "media", None) or []
    if not media:
        return {}
    m = media[0]
    parts = getattr(m, "parts", None) or []
    size = sum((getattr(p, "size", 0) or 0) for p in parts)
    return {
        "height": getattr(m, "height", None),
        "width": getattr(m, "width", None),
        "resolution": _resolution_bucket(getattr(m, "height", None)),
        "video_codec": getattr(m, "videoCodec", None),
        "audio_codec": getattr(m, "audioCodec", None),
        "container": getattr(m, "container", None),
        "bitrate_kbps": getattr(m, "bitrate", None),
        "file_size_bytes": size or None,
    }


def _parse_external_ids(item):
    ids = {}
    guids = []
    if getattr(item, "guid", None):
        guids.append(item.guid)
    for g in getattr(item, "guids", []) or []:
        guids.append(getattr(g, "id", "") or "")
    blob = " ".join(guids)
    for key, pat in GUID_PATTERNS.items():
        m = pat.search(blob)
        if m:
            ids[key] = m.group(1)
    return ids


def _export_movie(movie):
    out = {
        "type": "movie",
        "library": movie.librarySectionTitle,
        "title": movie.title,
        "original_title": getattr(movie, "originalTitle", None),
        "year": movie.year,
        "rating": movie.rating,
        "content_rating": getattr(movie, "contentRating", None),
        "duration_min": (movie.duration // 60000) if movie.duration else None,
        "studio": movie.studio,
        "genres": [g.tag for g in (movie.genres or [])],
        "directors": [d.tag for d in (movie.directors or [])],
        "added_at": _iso_or_none(movie.addedAt),
        "view_count": getattr(movie, "viewCount", 0) or 0,
        "rating_key": movie.ratingKey,
        "external_ids": _parse_external_ids(movie),
    }
    out.update(_media_summary(movie))
    return out


def _export_show(show):
    seasons = []
    for season in show.seasons():
        seasons.append({
            "season_number": season.index,
            "season_title": season.title,
            "episode_count": season.leafCount,
            "viewed_count": getattr(season, "viewedLeafCount", 0) or 0,
            "added_at": _iso_or_none(season.addedAt),
        })
    return {
        "type": "show",
        "library": show.librarySectionTitle,
        "title": show.title,
        "original_title": getattr(show, "originalTitle", None),
        "year": show.year,
        "rating": show.rating,
        "content_rating": getattr(show, "contentRating", None),
        "studio": show.studio,
        "genres": [g.tag for g in (show.genres or [])],
        "season_count": len(seasons),
        "episode_count": show.leafCount,
        "viewed_count": getattr(show, "viewedLeafCount", 0) or 0,
        "seasons": seasons,
        "added_at": _iso_or_none(show.addedAt),
        "rating_key": show.ratingKey,
        "external_ids": _parse_external_ids(show),
    }


def run_export(opts, log_fn=print):
    """Connect, scan, and return the payload dict. Raises on failure."""
    log_fn(f"connecting to {opts['plex_url']}")
    server = PlexServer(opts["plex_url"], opts["plex_token"], timeout=30)
    log_fn(f"connected to {server.friendlyName} (plex {server.version})")

    movies = []
    show_targets = []
    for section in server.library.sections():
        if section.type == "movie":
            log_fn(f"scanning movie library: {section.title}")
            for m in section.all():
                movies.append(_export_movie(m))
        elif section.type == "show":
            log_fn(f"queuing show library: {section.title}")
            show_targets.extend(section.all())
        else:
            log_fn(f"skipping section {section.title!r} (type={section.type})")

    shows = []
    if show_targets:
        workers = max(1, int(opts.get("parallel_jobs", 4)))
        log_fn(f"scanning {len(show_targets)} shows with {workers} workers")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for i, result in enumerate(ex.map(_export_show, show_targets), 1):
                shows.append(result)
                if i % 50 == 0:
                    log_fn(f"  …{i}/{len(show_targets)}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": {
            "name": server.friendlyName,
            "version": server.version,
            "machine_identifier": server.machineIdentifier,
        },
        "counts": {
            "movies": len(movies),
            "shows": len(shows),
            "seasons": sum(s["season_count"] for s in shows),
            "episodes": sum((s["episode_count"] or 0) for s in shows),
        },
        "movies": sorted(movies, key=lambda x: (x["title"] or "").lower()),
        "shows": sorted(shows, key=lambda x: (x["title"] or "").lower()),
    }
    return payload
