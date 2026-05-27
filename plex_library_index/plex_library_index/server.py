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

VERSION = "1.2.0"

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


async def send_notification(service: str, title: str, message: str):
    """Fire a notify.<service> call. Returns True on success."""
    if not service:
        return False
    # Strip optional "notify." prefix if user typed it
    if service.startswith("notify."):
        service = service[len("notify."):]
    payload = {"title": title, "message": message}
    status, body = await _ha_api_call(
        "POST", f"/services/notify/{service}", payload=payload
    )
    if status and 200 <= status < 300:
        logging.info(f"notification sent via notify.{service}")
        return True
    logging.warning(f"notify.{service} failed: status={status} body={body}")
    return False


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
        try:
            loop = asyncio.get_event_loop()
            payload = await loop.run_in_executor(
                None, lambda: exporter.run_export(opts, log_fn=logging.info)
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
            if opts.get("notify_on_error") and opts.get("notify_service"):
                await send_notification(
                    opts["notify_service"],
                    "Plex Library Index — scan failed",
                    f"Error: {e}",
                )
            if opts.get("telegram_enabled") and opts.get("telegram_notify_on_error"):
                await _telegram_broadcast(f"⚠️ Plex Library Index — scan failed\n\nError: {e}")
        finally:
            STATE["scanning"] = False
            save_state()


async def scheduled_loop():
    """Sleep then scan, repeatedly. Always re-reads options to pick up changes."""
    backoff = 60
    while True:
        opts = load_options()
        interval = max(1, int(opts.get("interval_hours", 24))) * 3600
        STATE["next_scan_after"] = (
            datetime.now(timezone.utc).timestamp() + interval
        )
        save_state()
        await asyncio.sleep(interval)
        try:
            await run_scan()
            backoff = 60
        except Exception:
            logging.exception("scheduled scan errored")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 1800)


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
