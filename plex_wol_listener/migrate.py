"""
Config migration for Plex WoL Listener add-on.

On startup, compares the stored schema version to CURRENT_SCHEMA_VERSION.
If outdated, remaps/drops fields in the add-on's saved options per FIELD_MAP,
POSTs the cleaned config to the Supervisor API, and writes the new version.

The saved options are read from the Supervisor (/addons/self/info), not from
/data/options.json: the Supervisor strips keys that aren't in the current
schema before writing that file, so renamed fields would never be seen there.

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
SUPERVISOR_URL = "http://supervisor"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# v4: re-run v3's renames now that old keys are actually readable
CURRENT_SCHEMA_VERSION = 4

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


def supervisor_request(path, payload=None):
    """GET (no payload) or POST JSON to the Supervisor API. Returns the response's `data`."""
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{SUPERVISOR_URL}{path}", data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    resp = urllib.request.urlopen(req, timeout=30)
    body = resp.read().decode()
    return (json.loads(body) if body else {}).get("data") or {}


def load_options():
    """Return the add-on's saved options, including fields no longer in the schema."""
    return supervisor_request("/addons/self/info").get("options", {})


def post_options(options):
    supervisor_request("/addons/self/options", {"options": options})
    print("[migrate] Config updated via Supervisor API", flush=True)


def apply_field_map(options):
    """Rename/drop fields in place. Returns True if anything changed."""
    changed = False
    for old_key, new_key in FIELD_MAP.items():
        if old_key not in options:
            continue
        value = options.pop(old_key)
        changed = True
        if new_key is None:
            print(f"[migrate] Dropping deprecated field: {old_key}", flush=True)
        elif options.get(new_key) in (None, ""):
            print(f"[migrate] Renaming field: {old_key} → {new_key}", flush=True)
            options[new_key] = value
        else:
            # The user has already set the new field; keep their value
            print(f"[migrate] Dropping {old_key}: {new_key} is already set", flush=True)
    return changed


def migrate():
    stored = get_stored_version()

    if stored >= CURRENT_SCHEMA_VERSION:
        return

    print(f"[migrate] Schema version {stored} → {CURRENT_SCHEMA_VERSION}", flush=True)

    if not SUPERVISOR_TOKEN:
        print("[migrate] WARNING: No SUPERVISOR_TOKEN — cannot read or update config", flush=True)
        return

    # Only record the new version once the migration has been saved, so a
    # failed attempt is retried on the next start (it's safe to repeat).
    try:
        options = load_options()
        if apply_field_map(options):
            post_options(options)
        else:
            print("[migrate] No field changes needed", flush=True)
    except Exception as e:
        print(f"[migrate] WARNING: Migration failed, will retry next start: {e}", flush=True)
        return

    set_stored_version(CURRENT_SCHEMA_VERSION)
    print("[migrate] Done", flush=True)


if __name__ == "__main__":
    migrate()
