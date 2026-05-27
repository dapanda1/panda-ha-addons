#!/usr/bin/env python3
"""Config migration for plex_library_index add-on.

Reads schema version from /data/.schema_version. If older than
CURRENT_SCHEMA_VERSION, runs sequential migrations to bring the
in-memory options dict forward, then pushes the result back to the
Supervisor API so the cleaned config is persisted.
"""
import json
import os
import sys
import urllib.error
import urllib.request

CURRENT_SCHEMA_VERSION = 3
SCHEMA_FILE = "/data/.schema_version"
OPTIONS_FILE = "/data/options.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
SUPERVISOR_URL = "http://supervisor/addons/self/options"


def log(msg):
    print(f"[migrate] {msg}", flush=True)


def read_schema_version() -> int:
    if not os.path.exists(SCHEMA_FILE):
        return 0
    try:
        return int(open(SCHEMA_FILE).read().strip())
    except (ValueError, OSError):
        return 0


def write_schema_version(v: int):
    with open(SCHEMA_FILE, "w") as f:
        f.write(str(v))


def load_options() -> dict:
    if not os.path.exists(OPTIONS_FILE):
        return {}
    with open(OPTIONS_FILE) as f:
        return json.load(f)


def push_options(opts: dict) -> bool:
    if not SUPERVISOR_TOKEN:
        log("no SUPERVISOR_TOKEN — skipping push")
        return False
    body = json.dumps({"options": opts}).encode()
    req = urllib.request.Request(
        SUPERVISOR_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            log(f"options pushed to supervisor: {r.status}")
            return True
    except urllib.error.HTTPError as e:
        log(f"supervisor push failed: HTTP {e.code} {e.reason}")
    except Exception as e:
        log(f"supervisor push failed: {e}")
    return False


def migrate_v0_to_v1(opts: dict) -> dict:
    """Initial schema. Backfill any missing keys with defaults."""
    opts.setdefault("plex_url", "http://homeassistant.local:32400")
    opts.setdefault("plex_token", "")
    opts.setdefault("interval_hours", 24)
    opts.setdefault("parallel_jobs", 4)
    opts.setdefault("gzip_output", True)
    opts.setdefault("log_level", "info")
    # drop any legacy keys here as they appear in future versions
    return opts


def migrate_v1_to_v2(opts: dict) -> dict:
    """Add notification config keys."""
    opts.setdefault("notify_on_success", False)
    opts.setdefault("notify_on_error", True)
    opts.setdefault("notify_service", "")
    opts.setdefault("notify_only_on_changes", False)
    return opts


def migrate_v2_to_v3(opts: dict) -> dict:
    """Add Telegram integration keys."""
    opts.setdefault("telegram_enabled", False)
    opts.setdefault("telegram_bot_token", "")
    opts.setdefault("telegram_allowed_chat_ids", [])
    opts.setdefault("telegram_notify_on_success", False)
    opts.setdefault("telegram_notify_on_error", True)
    opts.setdefault("telegram_answer_commands", True)
    return opts


MIGRATIONS = [
    (1, migrate_v0_to_v1),
    (2, migrate_v1_to_v2),
    (3, migrate_v2_to_v3),
]


def main():
    current = read_schema_version()
    if current >= CURRENT_SCHEMA_VERSION:
        log(f"schema v{current} up to date")
        return

    log(f"migrating schema v{current} -> v{CURRENT_SCHEMA_VERSION}")
    opts = load_options()
    for target_version, fn in MIGRATIONS:
        if current < target_version:
            opts = fn(opts)
            current = target_version
            log(f"applied migration to v{current}")

    push_options(opts)
    write_schema_version(current)
    log(f"now at schema v{current}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
