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


def _all_media_versions(item):
    """Return a list of dicts, one per Plex media version attached to the item.

    Plex stores duplicate copies of the same movie/episode as multiple
    `Media` objects under one ratingKey. Each has its own resolution,
    codec, container, file size, etc. This function captures all of them.
    """
    out = []
    for m in (getattr(item, "media", None) or []):
        parts = getattr(m, "parts", None) or []
        size = sum((getattr(p, "size", 0) or 0) for p in parts)
        out.append({
            "height": getattr(m, "height", None),
            "width": getattr(m, "width", None),
            "resolution": _resolution_bucket(getattr(m, "height", None)),
            "video_codec": getattr(m, "videoCodec", None),
            "audio_codec": getattr(m, "audioCodec", None),
            "container": getattr(m, "container", None),
            "bitrate_kbps": getattr(m, "bitrate", None),
            "file_size_bytes": size or None,
            "duration_min": (getattr(m, "duration", 0) // 60000) if getattr(m, "duration", None) else None,
        })
    return out


def _best_resolution(versions):
    """Return the highest resolution bucket present across all versions."""
    if not versions:
        return None
    order = {"4K": 4, "1080p": 3, "720p": 2, "SD": 1}
    best = None
    for v in versions:
        r = v.get("resolution")
        if r and (best is None or order.get(r, 0) > order.get(best, 0)):
            best = r
    return best


def _aggregate_media_summary(versions):
    """For backwards compatibility, pick the best version's stats as the
    item's top-level summary. The full `versions` list is exported alongside
    for clients that want per-copy info."""
    if not versions:
        return {}
    order = {"4K": 4, "1080p": 3, "720p": 2, "SD": 1}
    best = max(versions, key=lambda v: order.get(v.get("resolution") or "", 0))
    total_size = sum((v.get("file_size_bytes") or 0) for v in versions)
    return {
        "height": best.get("height"),
        "width": best.get("width"),
        "resolution": _best_resolution(versions),
        "video_codec": best.get("video_codec"),
        "audio_codec": best.get("audio_codec"),
        "container": best.get("container"),
        "bitrate_kbps": best.get("bitrate_kbps"),
        "file_size_bytes": total_size or None,
        "version_count": len(versions),
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
    versions = _all_media_versions(movie)
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
        "versions": versions,
    }
    out.update(_aggregate_media_summary(versions))
    return out


def _export_episode(ep):
    """Lightweight per-episode record. Only called when include_episodes=true."""
    versions = _all_media_versions(ep)
    out = {
        "season": ep.parentIndex,
        "episode": ep.index,
        "title": ep.title,
        "duration_min": (ep.duration // 60000) if ep.duration else None,
        "view_count": getattr(ep, "viewCount", 0) or 0,
        "added_at": _iso_or_none(ep.addedAt),
        "rating": ep.rating,
        "rating_key": ep.ratingKey,
        "versions": versions,
    }
    out.update(_aggregate_media_summary(versions))
    return out


def _compute_missing(present_numbers):
    """Given a sorted list of episode numbers present in a season, return
    a list of missing integers between the first and last present.

    E.g. [1, 2, 4, 7] -> [3, 5, 6]
    E.g. [1, 2, 3]    -> []
    E.g. []           -> []  (can't tell if anything is missing)
    """
    if len(present_numbers) < 2:
        return []
    nums = sorted(present_numbers)
    lo, hi = nums[0], nums[-1]
    present_set = set(nums)
    return [n for n in range(lo, hi + 1) if n not in present_set]


def _export_show(show, include_episodes=False, include_episode_numbers=True):
    """Export a show.

    - `include_episode_numbers=True` (default) makes one API call to enumerate
      episodes per season; only `(season, number)` tuples are kept. Used to
      compute missing-episode gaps per season.
    - `include_episodes=True` exports full per-episode metadata (much heavier).
      Implies `include_episode_numbers=True` regardless.
    """
    seasons = []
    for season in show.seasons():
        seasons.append({
            "season_number": season.index,
            "season_title": season.title,
            "episode_count": season.leafCount,
            "viewed_count": getattr(season, "viewedLeafCount", 0) or 0,
            "added_at": _iso_or_none(season.addedAt),
            "episode_numbers": None,   # filled below if enabled
            "missing_episodes": None,
        })

    episodes = None
    if include_episodes:
        try:
            episodes = [_export_episode(ep) for ep in show.episodes()]
        except Exception:
            episodes = []

    # Collect per-season episode numbers — for gap detection.
    if include_episodes or include_episode_numbers:
        try:
            if episodes is not None:
                # Already have full episodes; derive numbers from them
                ep_pairs = [(e.get("season"), e.get("episode")) for e in episodes
                            if e.get("season") is not None and e.get("episode") is not None]
            else:
                # Lightweight call — just episode numbers
                ep_pairs = []
                for ep in show.episodes():
                    s = ep.parentIndex
                    e = ep.index
                    if s is not None and e is not None:
                        ep_pairs.append((s, e))

            # Group by season
            by_season = {}
            for s, e in ep_pairs:
                by_season.setdefault(s, []).append(e)

            for season in seasons:
                sn = season["season_number"]
                if sn in by_season:
                    nums = sorted(set(by_season[sn]))
                    season["episode_numbers"] = nums
                    season["missing_episodes"] = _compute_missing(nums)
        except Exception:
            pass  # Don't kill the show export over this

    out = {
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
        "episodes": episodes,
        "added_at": _iso_or_none(show.addedAt),
        "rating_key": show.ratingKey,
        "external_ids": _parse_external_ids(show),
    }
    # Convenience: total missing-episode count across all seasons
    out["missing_episode_count"] = sum(
        len(s.get("missing_episodes") or []) for s in seasons
    )
    return out


def run_export(opts, log_fn=print, progress_fn=None):
    """Connect, scan, and return the payload dict. Raises on failure.

    `progress_fn`, if provided, is called with a dict describing the current
    phase, e.g. {'phase': 'movies', 'library': 'Movies', 'done': 123, 'total': None}
    or {'phase': 'shows', 'done': 50, 'total': 800}. Safe to ignore.
    """
    def _progress(payload):
        if progress_fn:
            try:
                progress_fn(payload)
            except Exception:
                pass

    log_fn(f"connecting to {opts['plex_url']}")
    server = PlexServer(opts["plex_url"], opts["plex_token"], timeout=30)
    log_fn(f"connected to {server.friendlyName} (plex {server.version})")
    _progress({"phase": "connected"})

    movies = []
    show_targets = []
    for section in server.library.sections():
        if section.type == "movie":
            log_fn(f"scanning movie library: {section.title}")
            _progress({"phase": "movies", "library": section.title, "done": 0})
            section_movies = section.all()
            total = len(section_movies)
            for i, m in enumerate(section_movies, 1):
                movies.append(_export_movie(m))
                if i % 50 == 0 or i == total:
                    log_fn(f"  …movies {i}/{total} from {section.title}")
                    _progress({"phase": "movies", "library": section.title,
                               "done": len(movies), "total": total})
        elif section.type == "show":
            log_fn(f"queuing show library: {section.title}")
            show_targets.extend(section.all())
        else:
            log_fn(f"skipping section {section.title!r} (type={section.type})")

    shows = []
    if show_targets:
        workers = max(1, int(opts.get("parallel_jobs", 4)))
        include_eps = bool(opts.get("include_episodes", False))
        include_ep_nums = bool(opts.get("include_episode_numbers", True))
        total_shows = len(show_targets)
        if include_eps:
            eps_note = " (with full episode detail)"
        elif include_ep_nums:
            eps_note = " (with gap detection)"
        else:
            eps_note = ""
        log_fn(f"scanning {total_shows} shows with {workers} workers{eps_note}")
        _progress({"phase": "shows", "done": 0, "total": total_shows})

        def _export_one(show):
            return _export_show(show, include_episodes=include_eps,
                                include_episode_numbers=include_ep_nums)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for i, result in enumerate(ex.map(_export_one, show_targets), 1):
                shows.append(result)
                if i % 50 == 0 or i == total_shows:
                    log_fn(f"  …shows {i}/{total_shows}")
                    _progress({"phase": "shows", "done": i, "total": total_shows})

    _progress({"phase": "writing"})

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": {
            "name": server.friendlyName,
            "version": server.version,
            "machine_identifier": server.machineIdentifier,
        },
        "features": {
            "episodes_indexed": bool(opts.get("include_episodes", False)),
            "episode_numbers_indexed": bool(
                opts.get("include_episode_numbers", True)
                or opts.get("include_episodes", False)
            ),
        },
        "counts": {
            "movies": len(movies),
            "shows": len(shows),
            "seasons": sum(s["season_count"] for s in shows),
            "episodes": sum((s["episode_count"] or 0) for s in shows),
            "episodes_indexed": sum(
                len(s.get("episodes") or []) for s in shows
            ) if opts.get("include_episodes") else 0,
            "missing_episodes": sum(s.get("missing_episode_count", 0) for s in shows),
            "shows_with_gaps": sum(
                1 for s in shows if s.get("missing_episode_count", 0) > 0
            ),
            "movie_versions": sum(len(m.get("versions") or []) for m in movies),
        },
        "movies": sorted(movies, key=lambda x: (x["title"] or "").lower()),
        "shows": sorted(shows, key=lambda x: (x["title"] or "").lower()),
    }
    return payload
