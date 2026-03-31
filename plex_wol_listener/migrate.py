"""
Config migration for Plex WoL Listener add-on.

On startup, compares the stored schema version to CURRENT_SCHEMA_VERSION.
If outdated, remaps/drops fields in /data/options.json per FIELD_MAP,
POSTs the cleaned config to the Supervisor API, and writes the new version.

To add a migration in the future:
1. Bump CURRENT_SCHEMA_VERSION
2. Add old_name -> new_name (or old_name -> None to drop) entries to FIELD_MAP
3. Update config.json options/schema
4. Bump config.json version
"""

import json
import os
import urllib.request

SCHEMA_VERSION_FILE = "/data/.schema_version"
OPTIONS_PATH = "/data/options.json"
SUPERVISOR_API = "http://supervisor/addons/self/options"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

CURRENT_SCHEMA_VERSION = 3

# Keys: old field name → new field name (str) or None to drop.
# Only add entries here when a field is renamed or removed.
FIELD_MAP = {
    "enable_token_validation": None,
    "nowake_exclude": "allow_ip_plex_relay",
    "infra_learn_threshold": None,
    "infra_learn_window_hours": None,
    "enable_dashboard_toggles": None,
}


def get_stored_version():
    try:
        with open(SCHEMA_VERSION_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def set_stored_version(version):
    try:
        with open(SCHEMA_VERSION_FILE, "w") as f:
            f.write(str(version))
    except Exception as e:
        print(f"[migrate] WARNING: Could not write schema version: {e}", flush=True)


def load_options():
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"[migrate] WARNING: Could not read {OPTIONS_PATH}: {e}", flush=True)
        return {}


def post_options(options):
    """POST cleaned options to Supervisor API."""
    if not SUPERVISOR_TOKEN:
        print("[migrate] WARNING: No SUPERVISOR_TOKEN — cannot update config via API", flush=True)
        return False
    try:
        payload = json.dumps({"options": options}).encode()
        req = urllib.request.Request(
            SUPERVISOR_API,
            data=payload,
            headers={
                "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        if resp.status == 200:
            print("[migrate] Config updated via Supervisor API", flush=True)
            return True
        else:
            print(f"[migrate] WARNING: Supervisor API returned status {resp.status}", flush=True)
            return False
    except Exception as e:
        print(f"[migrate] WARNING: Failed to POST config: {e}", flush=True)
        return False


def migrate():
    stored = get_stored_version()

    if stored >= CURRENT_SCHEMA_VERSION:
        return

    print(f"[migrate] Schema version {stored} → {CURRENT_SCHEMA_VERSION}", flush=True)

    options = load_options()
    changed = False

    for old_key, new_key in FIELD_MAP.items():
        if old_key in options:
            if new_key is None:
                # Drop the field
                print(f"[migrate] Dropping deprecated field: {old_key}", flush=True)
                del options[old_key]
                changed = True
            elif new_key != old_key:
                # Rename the field
                print(f"[migrate] Renaming field: {old_key} → {new_key}", flush=True)
                options[new_key] = options.pop(old_key)
                changed = True

    if changed:
        if post_options(options):
            set_stored_version(CURRENT_SCHEMA_VERSION)
        else:
            print("[migrate] WARNING: Migration applied locally but API update failed", flush=True)
            # Still write version to avoid retrying every restart
            set_stored_version(CURRENT_SCHEMA_VERSION)
    else:
        print("[migrate] No field changes needed", flush=True)
        set_stored_version(CURRENT_SCHEMA_VERSION)

    print("[migrate] Done", flush=True)


if __name__ == "__main__":
    migrate()
