#!/usr/bin/env python3
"""Plex Library Index — aiohttp server for HA add-on.

Serves the search page through HA ingress and runs scheduled exports.
Endpoints:
  GET  /                      -> search page
  GET  /library.json[.gz]     -> the exported data
  GET  /api/status            -> JSON: scan state, last run, counts
  POST /api/refresh           -> start a scan (returns 409 if one is running)
  GET  /api/preferences       -> UI preferences (per-device)
  PUT  /api/preferences       -> update UI preferences
  GET  /api/notify-services   -> list available HA notify.* services
  GET  /api/config-summary    -> non-secret config view for the prefs page
"""
import asyncio
import gzip
import json
import logging
import os
import shutil
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

import exporter
import telegram_bot

VERSION = "1.4.4"

OPTIONS_PATH = Path("/data/options.json")
WWW_DIR = Path("/data/www")
SRC_WWW = Path("/usr/src/www")
LIBRARY_JSON_GZ = WWW_DIR / "library.json.gz"
LIBRARY_JSON = WWW_DIR / "library.json"
STATE_PATH = Path("/data/state.json")
PREFS_PATH = Path("/data/preferences.json")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_API_BASE = "http://supervisor/core/api"

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
          "warning": logging.WARNING, "error": logging.ERROR}

DEFAULT_PREFS = {
    "default_sort": "title",
    "default_type": "all",
    "default_library": "",
    "batch_size": 50,
    "hide_watched_default": False,
    "show_4k_only_default": False,
    "show_recent_default": False,
    "auto_refresh_on_open": False,
    "browser_notify_on_success": False,
    "browser_notify_on_error": True,
}

# Mutable global state, read by /api/status
STATE = {
    "scanning": False,
    "last_scan": None,
    "last_error": None,
    "last_duration_s": None,
    "counts": None,
    "previous_counts": None,
    "next_scan_after": None,
    "version": VERSION,
    "retry_after": None,
    "consecutive_failures": 0,
    "outage_started_at": None,      # ISO timestamp — set on first notified failure of an outage
    "outage_last_alerted_at": None, # ISO timestamp — set every time we send an outage alert
    "progress": None,
}
scan_lock = asyncio.Lock()

# Cached in-memory copy of the most recent library payload (also written to
# disk as library.json[.gz]). The Telegram handler reads from this directly
# to avoid hitting disk on every query.
LIBRARY_CACHE = {"payload": None, "loaded_at": None}


def _load_library_from_disk():
    """Load library payload from disk (gz preferred). Returns dict or None."""
    if LIBRARY_JSON_GZ.exists():
        try:
            with gzip.open(LIBRARY_JSON_GZ, "rb") as f:
                return json.loads(f.read().decode("utf-8"))
        except Exception as e:
            logging.warning(f"could not read {LIBRARY_JSON_GZ}: {e}")
    if LIBRARY_JSON.exists():
        try:
            return json.loads(LIBRARY_JSON.read_text())
        except Exception as e:
            logging.warning(f"could not read {LIBRARY_JSON}: {e}")
    return None


def get_library_payload():
    """Return the cached library payload, refreshing from disk if needed."""
    if LIBRARY_CACHE["payload"] is None:
        LIBRARY_CACHE["payload"] = _load_library_from_disk()
        LIBRARY_CACHE["loaded_at"] = datetime.now(timezone.utc).isoformat()
    return LIBRARY_CACHE["payload"]


def invalidate_library_cache():
    LIBRARY_CACHE["payload"] = None
    LIBRARY_CACHE["loaded_at"] = None


def load_options():
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def load_prefs():
    if not PREFS_PATH.exists():
        return dict(DEFAULT_PREFS)
    try:
        data = json.loads(PREFS_PATH.read_text())
        merged = dict(DEFAULT_PREFS)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_PREFS})
        return merged
    except Exception as e:
        logging.warning(f"could not load preferences, using defaults: {e}")
        return dict(DEFAULT_PREFS)


def save_prefs(prefs: dict):
    PREFS_PATH.write_text(json.dumps(prefs, indent=2))


def setup_logging(level_name):
    logging.basicConfig(
        level=LEVELS.get(level_name, logging.INFO),
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def save_state():
    try:
        STATE_PATH.write_text(json.dumps(STATE))
    except Exception as e:
        logging.warning(f"could not persist state: {e}")


def load_state():
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
        # Don't restore "scanning: True" across restarts
        data["scanning"] = False
        STATE.update(data)
    except Exception as e:
        logging.warning(f"could not load saved state: {e}")


def write_payload(payload, gzip_output):
    WWW_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    target = LIBRARY_JSON_GZ if gzip_output else LIBRARY_JSON
    other = LIBRARY_JSON if gzip_output else LIBRARY_JSON_GZ
    tmp = target.with_suffix(target.suffix + ".tmp")

    if gzip_output:
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            f.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    os.replace(tmp, target)

    if other.exists():
        try:
            other.unlink()
        except Exception:
            pass
    return target


def ensure_index_html():
    WWW_DIR.mkdir(parents=True, exist_ok=True)
    src = SRC_WWW / "index.html"
    dst = WWW_DIR / "index.html"
    if not src.exists():
        logging.warning(f"missing shipped index.html at {src}")
        return
    if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
        shutil.copy(src, dst)
        logging.info(f"installed {dst}")


# --- Home Assistant API helpers (for notifications) ---

async def _ha_api_call(method: str, path: str, payload=None):
    """Call HA Core REST API via Supervisor proxy. Returns (status, json_or_text)."""
    if not SUPERVISOR_TOKEN:
        return None, "SUPERVISOR_TOKEN unavailable"
    url = f"{HA_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, json=payload, headers=headers) as r:
                text = await r.text()
                try:
                    body = json.loads(text) if text else None
                except json.JSONDecodeError:
                    body = text
                return r.status, body
    except Exception as e:
        return None, str(e)


async def list_notify_services():
    """Return a sorted list of available notify.* service names (without prefix)."""
    status, body = await _ha_api_call("GET", "/services")
    if status != 200 or not isinstance(body, list):
        return []
    services = []
    for domain_entry in body:
        if domain_entry.get("domain") == "notify":
            for svc_name in (domain_entry.get("services") or {}).keys():
                services.append(svc_name)
    return sorted(services)


async def send_notification(service, title: str, message: str):
    """Fire one or more notify.<service> calls.

    `service` may be a single service name (e.g. "mobile_app_pixel_8") or
    a comma-separated list (e.g. "mobile_app_pixel_8, persistent_notification").
    Each service in the list is called independently — failures on one don't
    block others. Returns True if at least one succeeded.
    """
    if not service:
        return False
    # Accept comma- or whitespace-separated lists. Strip optional notify. prefix.
    raw_parts = [p.strip() for p in service.replace(";", ",").split(",")]
    services = []
    for p in raw_parts:
        if not p:
            continue
        if p.startswith("notify."):
            p = p[len("notify."):]
        services.append(p)
    if not services:
        return False

    payload = {"title": title, "message": message}
    any_ok = False
    for svc in services:
        status, body = await _ha_api_call(
            "POST", f"/services/notify/{svc}", payload=payload
        )
        if status and 200 <= status < 300:
            logging.info(f"notification sent via notify.{svc}")
            any_ok = True
        else:
            logging.warning(f"notify.{svc} failed: status={status} body={body}")
    return any_ok


def _format_summary(counts, duration_s):
    parts = [
        f"{counts.get('movies', 0)} movies",
        f"{counts.get('shows', 0)} shows",
        f"{counts.get('seasons', 0)} seasons",
    ]
    return ", ".join(parts) + f" — scan took {duration_s:.0f}s"


def _diff_counts(prev, curr):
    """Returns a human-readable diff string, or empty if no change."""
    if not prev:
        return ""
    deltas = []
    for key in ("movies", "shows", "seasons", "episodes"):
        d = (curr.get(key, 0) or 0) - (prev.get(key, 0) or 0)
        if d:
            sign = "+" if d > 0 else ""
            deltas.append(f"{sign}{d} {key}")
    return ", ".join(deltas)


# --- Telegram integration glue ---

# Reference to the live TelegramClient (set by run_polling_loop on_ready).
TELEGRAM_CLIENT = {"instance": None}


async def _telegram_broadcast(text: str):
    """Send a message to every allowed Telegram chat. Safe to call even
    when Telegram is disabled — silently no-ops."""
    client = TELEGRAM_CLIENT.get("instance")
    if not client:
        return 0
    try:
        return await client.broadcast(text)
    except Exception as e:
        logging.warning(f"telegram broadcast failed: {e}")
        return 0


async def _telegram_on_ready(client):
    TELEGRAM_CLIENT["instance"] = client


async def run_scan():
    if scan_lock.locked():
        logging.info("scan already in progress, skipping")
        return
    async with scan_lock:
        STATE["scanning"] = True
        STATE["last_error"] = None
        save_state()
        opts = load_options()
        if not opts.get("plex_token"):
            STATE["scanning"] = False
            STATE["last_error"] = "plex_token not configured"
            save_state()
            logging.error("plex_token not configured")
            if opts.get("notify_on_error") and opts.get("notify_service"):
                await send_notification(
                    opts["notify_service"],
                    "Plex Library Index — config error",
                    "plex_token is not set in the add-on configuration.",
                )
            if opts.get("telegram_enabled") and opts.get("telegram_notify_on_error"):
                await _telegram_broadcast(
                    "⚠️ Plex Library Index — config error\n\n"
                    "plex_token is not set in the add-on configuration."
                )
            return
        started = datetime.now(timezone.utc)
        previous_counts = STATE.get("counts")
        STATE["progress"] = {"phase": "connecting"}
        save_state()

        def _progress(p):
            STATE["progress"] = p

        try:
            loop = asyncio.get_event_loop()
            payload = await loop.run_in_executor(
                None, lambda: exporter.run_export(
                    opts, log_fn=logging.info, progress_fn=_progress
                )
            )
            target = write_payload(payload, opts.get("gzip_output", True))
            size_kb = target.stat().st_size / 1024
            counts = payload.get("counts", {})
            duration = (datetime.now(timezone.utc) - started).total_seconds()
            logging.info(
                f"wrote {target} ({size_kb:.1f} KB) in {duration:.1f}s — "
                f"{counts.get('movies',0)} movies, {counts.get('shows',0)} shows, "
                f"{counts.get('seasons',0)} seasons"
            )
            STATE["last_scan"] = started.isoformat()
            STATE["last_duration_s"] = round(duration, 1)
            STATE["previous_counts"] = previous_counts
            STATE["counts"] = counts
            # Successful scan — reset retry backoff and clear outage tracking
            STATE["retry_after"] = None
            STATE["consecutive_failures"] = 0
            STATE["outage_started_at"] = None
            STATE["outage_last_alerted_at"] = None

            # Refresh cache so Telegram queries see new data
            invalidate_library_cache()

            diff = _diff_counts(previous_counts, counts)
            only_changes = opts.get("notify_only_on_changes", False)
            should_notify_success = (not only_changes) or bool(diff)

            # HA notify (existing path)
            if opts.get("notify_on_success") and opts.get("notify_service"):
                if should_notify_success:
                    msg = _format_summary(counts, duration)
                    if diff:
                        msg = f"Changes: {diff}\n{msg}"
                    await send_notification(
                        opts["notify_service"],
                        "Plex Library Index — scan complete",
                        msg,
                    )
                else:
                    logging.debug("notify_only_on_changes set and no changes — skipping HA notification")

            # Telegram (new path)
            if opts.get("telegram_enabled") and opts.get("telegram_notify_on_success"):
                if should_notify_success:
                    body = "✅ Plex Library Index — scan complete\n\n"
                    body += _format_summary(counts, duration)
                    if diff:
                        body = f"✅ Plex Library Index — changes: {diff}\n\n" + _format_summary(counts, duration)
                    await _telegram_broadcast(body)
                else:
                    logging.debug("notify_only_on_changes set and no changes — skipping Telegram notification")

        except Exception as e:
            logging.exception("export failed")
            STATE["last_error"] = str(e)

            # Detect "Plex unreachable" style errors and schedule a quick retry.
            # Cap at 2 quick retries — if both fail, give up and wait for the
            # next normally scheduled scan.
            MAX_QUICK_RETRIES = 2
            is_connection_error = _is_connection_error(e)
            if is_connection_error:
                fails = STATE.get("consecutive_failures", 0) + 1
                STATE["consecutive_failures"] = fails
                if fails <= MAX_QUICK_RETRIES:
                    # 1st retry at 5min, 2nd retry at 10min
                    delay = 300 if fails == 1 else 600
                    retry_at = datetime.now(timezone.utc).timestamp() + delay
                    STATE["retry_after"] = retry_at
                    logging.info(
                        f"plex unreachable — quick retry #{fails}/{MAX_QUICK_RETRIES} "
                        f"scheduled in {delay}s"
                    )
                else:
                    # Out of quick retries — fall back to normal schedule
                    STATE["retry_after"] = None
                    logging.info(
                        f"plex unreachable for {fails} attempts — giving up quick "
                        f"retries; next attempt at scheduled scan time"
                    )

            # Decide whether to send a failure notification.
            #
            # Rules (see _should_notify_failure):
            # - Non-connection errors always notify.
            # - First failure of a Plex-unreachable outage always notifies.
            # - During an ongoing outage, only re-notify on Sundays (local time),
            #   and only if at least 3 days have passed since the last alert.
            # Successful scans clear the outage state so the next failure
            # notifies fresh.
            should_notify_failure = _should_notify_failure(is_connection_error)
            if should_notify_failure:
                now_iso = datetime.now(timezone.utc).isoformat()
                if STATE.get("outage_started_at") is None:
                    STATE["outage_started_at"] = now_iso
                STATE["outage_last_alerted_at"] = now_iso

                if opts.get("notify_on_error") and opts.get("notify_service"):
                    await send_notification(
                        opts["notify_service"],
                        "Plex Library Index — scan failed",
                        f"Error: {e}",
                    )
                if opts.get("telegram_enabled") and opts.get("telegram_notify_on_error"):
                    await _telegram_broadcast(f"⚠️ Plex Library Index — scan failed\n\nError: {e}")
            else:
                logging.debug(
                    "suppressing failure notification per weekly-reminder policy "
                    f"(consecutive failures: {STATE.get('consecutive_failures')})"
                )
        finally:
            STATE["scanning"] = False
            STATE["progress"] = None
            save_state()


def _is_connection_error(exc):
    """Detect errors caused by Plex server being unreachable, vs other errors."""
    err_str = str(exc).lower()
    indicators = [
        "host is unreachable",
        "connection refused",
        "connection error",
        "name or service not known",
        "no route to host",
        "timed out",
        "max retries exceeded",
        "failed to establish",
    ]
    return any(s in err_str for s in indicators)


def _should_notify_failure(is_connection_error):
    """Decide whether to send a notification for a scan failure.

    Policy:
    - Non-connection errors (auth failure, bad config, unexpected exceptions):
      always notify — these usually need attention regardless of history.
    - First failure of a Plex-unreachable outage: always notify.
    - Subsequent failures during the same outage: notify only when today is
      Sunday in the local timezone AND the last alert was at least 3 days ago.
      Outage state clears on the next successful scan.

    This gives one notification when an outage begins and a weekly Sunday
    reminder while the server stays down, without spamming on every scan.
    """
    if not is_connection_error:
        return True

    last_alerted = STATE.get("outage_last_alerted_at")
    if not last_alerted:
        # Either no prior outage or this is the first failure of one.
        return True

    # Fail open — if the timestamp parses badly, don't accidentally go silent.
    try:
        last_dt = datetime.fromisoformat(last_alerted)
    except (ValueError, TypeError):
        return True

    now_local = datetime.now().astimezone()
    if now_local.weekday() != 6:  # 0=Mon .. 6=Sun
        return False

    # Compare in aware datetimes; convert stored UTC to local
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    days_since = (now_local - last_dt.astimezone()).total_seconds() / 86400.0
    return days_since >= 3.0


def _parse_hhmm(s):
    """Parse 'HH:MM' into (hour, minute) tuple, or None on bad input."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    try:
        h_str, m_str = s.split(":", 1)
        h, m = int(h_str), int(m_str)
        if 0 <= h < 24 and 0 <= m < 60:
            return (h, m)
    except (ValueError, AttributeError):
        pass
    return None


def _next_scheduled_run(opts, now=None):
    """Compute the next scheduled scan time as a unix timestamp.

    Uses scan_time and scan_time_2 (HH:MM strings, local time) when set;
    falls back to interval_hours-from-now if both are empty.

    Returns (timestamp, reason_string).
    """
    if now is None:
        now = datetime.now().astimezone()  # local timezone

    times = []
    for key in ("scan_time", "scan_time_2"):
        t = _parse_hhmm(opts.get(key))
        if t:
            times.append(t)

    if not times:
        interval = max(1, int(opts.get("interval_hours", 24))) * 3600
        return (now.timestamp() + interval, f"every {opts.get('interval_hours', 24)}h")

    # Build candidate datetimes: today's HH:MM and tomorrow's HH:MM for each
    # configured time, pick the earliest one that's still in the future.
    candidates = []
    for h, m in times:
        for day_offset in (0, 1):
            d = now.replace(hour=h, minute=m, second=0, microsecond=0)
            from datetime import timedelta
            d = d + timedelta(days=day_offset)
            if d > now:
                candidates.append((d, h, m))
    if not candidates:
        # Shouldn't happen given the day_offset=1 fallback, but be safe
        return (now.timestamp() + 3600, "fallback (1h)")

    candidates.sort(key=lambda x: x[0])
    chosen, h, m = candidates[0]
    return (chosen.timestamp(), f"daily at {h:02d}:{m:02d}")


async def scheduled_loop():
    """Sleep until the next scheduled scan time, then scan. Repeat.

    Two scheduling modes:
    - Time-of-day (preferred): if scan_time / scan_time_2 are set, sleeps
      until the next one comes around. Predictable, runs at the same wall
      clock time every day.
    - Interval fallback: if both scan times are blank, sleeps interval_hours
      from now between scans.

    Connection-failure quick retry (STATE['retry_after']) can shorten the
    next wake-up, but only fires twice — after that, retry_after is cleared
    and we wait for the normal schedule.
    """
    while True:
        opts = load_options()
        now = datetime.now(timezone.utc).timestamp()
        next_normal, reason = _next_scheduled_run(opts)

        # Quick-retry override: if a retry is scheduled and it's sooner
        # than the next normal scan, honor it.
        retry_after = STATE.get("retry_after")
        next_wake = next_normal
        wake_reason = reason
        if retry_after and retry_after < next_normal:
            next_wake = retry_after
            wake_reason = "quick retry"

        STATE["next_scan_after"] = next_wake
        save_state()

        sleep_for = max(1, next_wake - now)
        logging.info(
            f"next scan at {datetime.fromtimestamp(next_wake).astimezone().isoformat(timespec='seconds')} "
            f"({wake_reason}, in {int(sleep_for)}s)"
        )

        await asyncio.sleep(sleep_for)
        try:
            await run_scan()
        except Exception:
            logging.exception("scheduled scan errored")


# --- HTTP handlers ---

async def handle_index(request):
    path = WWW_DIR / "index.html"
    if not path.exists():
        return web.Response(text="index.html missing", status=500)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def handle_library_gz(request):
    if not LIBRARY_JSON_GZ.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(
        LIBRARY_JSON_GZ,
        headers={
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )


async def handle_library_json(request):
    if not LIBRARY_JSON.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(
        LIBRARY_JSON,
        headers={"Cache-Control": "no-cache", "Content-Type": "application/json"},
    )


async def handle_status(request):
    return web.json_response(STATE)


async def handle_refresh(request):
    if scan_lock.locked():
        return web.json_response(
            {"ok": False, "message": "scan already running"}, status=409
        )
    asyncio.create_task(run_scan())
    return web.json_response({"ok": True, "message": "scan started"})


async def handle_health(request):
    return web.json_response({"ok": True})


async def handle_get_prefs(request):
    return web.json_response(load_prefs())


async def handle_put_prefs(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "message": "expected object"}, status=400)
    current = load_prefs()
    # Whitelist-validate against DEFAULT_PREFS keys/types
    for k, v in body.items():
        if k not in DEFAULT_PREFS:
            continue
        expected_type = type(DEFAULT_PREFS[k])
        # Accept ints for bools? No — keep types strict but tolerate JSON quirks.
        if expected_type is bool:
            current[k] = bool(v)
        elif expected_type is int:
            try:
                current[k] = int(v)
            except (TypeError, ValueError):
                pass
        else:
            current[k] = str(v) if v is not None else ""
    save_prefs(current)
    return web.json_response({"ok": True, "preferences": current})


async def handle_notify_services(request):
    services = await list_notify_services()
    return web.json_response({"services": services})


async def handle_config_summary(request):
    """Return the non-secret subset of config so the prefs page can display it."""
    opts = load_options()
    return web.json_response({
        "plex_url": opts.get("plex_url", ""),
        "plex_token_configured": bool(opts.get("plex_token")),
        "interval_hours": opts.get("interval_hours", 24),
        "parallel_jobs": opts.get("parallel_jobs", 4),
        "gzip_output": opts.get("gzip_output", True),
        "log_level": opts.get("log_level", "info"),
        "notify_on_success": opts.get("notify_on_success", False),
        "notify_on_error": opts.get("notify_on_error", True),
        "notify_service": opts.get("notify_service", ""),
        "notify_only_on_changes": opts.get("notify_only_on_changes", False),
        "telegram_enabled": opts.get("telegram_enabled", False),
        "telegram_bot_token_configured": bool(opts.get("telegram_bot_token")),
        "telegram_allowed_chat_ids_count": len(opts.get("telegram_allowed_chat_ids") or []),
        "telegram_notify_on_success": opts.get("telegram_notify_on_success", False),
        "telegram_notify_on_error": opts.get("telegram_notify_on_error", True),
        "telegram_answer_commands": opts.get("telegram_answer_commands", True),
        "telegram_connected": TELEGRAM_CLIENT.get("instance") is not None,
    })


async def handle_test_notify(request):
    opts = load_options()
    service = opts.get("notify_service")
    if not service:
        return web.json_response(
            {"ok": False, "message": "notify_service not configured in add-on options"},
            status=400,
        )
    ok = await send_notification(
        service,
        "Plex Library Index — test notification",
        "This is a test notification from the Plex Library Index add-on.",
    )
    return web.json_response(
        {"ok": ok, "service": service,
         "message": "notification sent" if ok else "send failed (see add-on logs)"}
    )


async def handle_test_telegram(request):
    opts = load_options()
    if not opts.get("telegram_enabled"):
        return web.json_response(
            {"ok": False, "message": "telegram_enabled is false"}, status=400)
    if not opts.get("telegram_bot_token"):
        return web.json_response(
            {"ok": False, "message": "telegram_bot_token not set"}, status=400)
    client = TELEGRAM_CLIENT.get("instance")
    if not client:
        return web.json_response(
            {"ok": False, "message": "telegram client not connected yet — wait a few seconds"},
            status=503,
        )
    if not client.allowed:
        return web.json_response(
            {"ok": False, "message": "telegram_allowed_chat_ids is empty"}, status=400)
    sent = await client.broadcast(
        "🧪 Plex Library Index — test message\n\n"
        "If you see this, the bot is wired up correctly."
    )
    return web.json_response(
        {"ok": sent > 0, "sent_to": sent,
         "message": f"sent to {sent} chat(s)" if sent else "no messages delivered (check chat IDs)"}
    )


async def handle_clear_library(request):
    """Delete the library JSON file(s) and reset scan state, so the next
    scan starts completely from scratch. Does NOT touch config/preferences."""
    if scan_lock.locked():
        return web.json_response(
            {"ok": False, "message": "scan in progress — wait for it to finish first"},
            status=409,
        )

    removed = []
    for path in (LIBRARY_JSON_GZ, LIBRARY_JSON):
        if path.exists():
            try:
                path.unlink()
                removed.append(path.name)
            except Exception as e:
                logging.warning(f"could not remove {path}: {e}")

    # Reset scan-related state. Keep retry_after / consecutive_failures cleared
    # too, since this is a deliberate reset.
    STATE["last_scan"] = None
    STATE["last_error"] = None
    STATE["last_duration_s"] = None
    STATE["counts"] = None
    STATE["previous_counts"] = None
    STATE["retry_after"] = None
    STATE["consecutive_failures"] = 0
    STATE["outage_started_at"] = None
    STATE["outage_last_alerted_at"] = None
    STATE["progress"] = None
    save_state()

    # Drop the in-memory cache too
    invalidate_library_cache()

    logging.info(f"library cleared (removed: {', '.join(removed) if removed else 'no files present'})")
    return web.json_response({
        "ok": True,
        "removed": removed,
        "message": f"library cleared ({len(removed)} file(s) removed)" if removed
                   else "library was already empty",
    })


def build_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/index.html", handle_index)
    app.router.add_get("/library.json.gz", handle_library_gz)
    app.router.add_get("/library.json", handle_library_json)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/refresh", handle_refresh)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/preferences", handle_get_prefs)
    app.router.add_put("/api/preferences", handle_put_prefs)
    app.router.add_get("/api/notify-services", handle_notify_services)
    app.router.add_get("/api/config-summary", handle_config_summary)
    app.router.add_post("/api/test-notify", handle_test_notify)
    app.router.add_post("/api/test-telegram", handle_test_telegram)
    app.router.add_post("/api/clear-library", handle_clear_library)
    return app


async def main():
    opts = load_options()
    setup_logging(opts.get("log_level", "info"))
    logging.info(f"starting Plex Library Index v{VERSION}")
    ensure_index_html()
    load_state()

    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8099)
    await site.start()
    logging.info("listening on 0.0.0.0:8099 (ingress)")

    # Initial scan if no data yet
    if not LIBRARY_JSON_GZ.exists() and not LIBRARY_JSON.exists():
        logging.info("no existing data — starting initial scan")
        asyncio.create_task(run_scan())
    else:
        logging.info("existing data found — will scan on schedule")

    # Schedule loop
    schedule_task = asyncio.create_task(scheduled_loop())

    # Telegram polling loop (no-ops if not enabled)
    telegram_stop = asyncio.Event()
    telegram_task = asyncio.create_task(telegram_bot.run_polling_loop(
        get_options=load_options,
        get_library=get_library_payload,
        get_state=lambda: dict(STATE),
        on_ready=_telegram_on_ready,
        stop_event=telegram_stop,
    ))

    # Wait for shutdown
    stop = asyncio.Event()

    def _stop(*_):
        logging.info("shutdown requested")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop)
    await stop.wait()

    telegram_stop.set()
    schedule_task.cancel()
    # Give telegram a moment to exit cleanly from its long-poll
    try:
        await asyncio.wait_for(telegram_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        telegram_task.cancel()
    await runner.cleanup()
    logging.info("exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
