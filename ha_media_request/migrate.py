"""
Config migration for HA Media Request add-on.

On startup, compares the stored schema version to CURRENT_SCHEMA_VERSION.
If outdated, remaps/drops fields per FIELD_MAP and POSTs the cleaned
config back to the Supervisor API. If versions match, does nothing.

To add a migration in the future:
  1. Bump CURRENT_SCHEMA_VERSION
  2. Update FIELD_MAP with old_name → new_name (or old_name → None to drop)
  3. Update options/schema in config.json
  4. Bump the add-on version in config.json
"""

import json
import os
import logging
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("migrate")

# ── Configuration ────────────────────────────────────────────────────
CURRENT_SCHEMA_VERSION = 1

# Maps old field names → new field names. Set value to None to drop a field.
# Example for a future migration:
#   FIELD_MAP = {
#       "old_field_name": "new_field_name",
#       "deprecated_field": None,
#   }
FIELD_MAP = {}

# ── Paths ────────────────────────────────────────────────────────────
SCHEMA_VERSION_FILE = "/data/.schema_version"
OPTIONS_PATH = "/data/options.json"
SUPERVISOR_API = "http://supervisor/addons/self/options"


def get_stored_version():
    """Read the stored schema version from disk. Returns 0 if not set."""
    if os.path.exists(SCHEMA_VERSION_FILE):
        try:
            with open(SCHEMA_VERSION_FILE, "r") as f:
                return int(f.read().strip())
        except (ValueError, IOError):
            pass
    return 0


def set_stored_version(version):
    """Write the schema version to disk."""
    with open(SCHEMA_VERSION_FILE, "w") as f:
        f.write(str(version))


def load_options():
    """Load current options from disk."""
    with open(OPTIONS_PATH, "r") as f:
        return json.load(f)


def post_options(options):
    """POST updated options to the Supervisor API."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # The Supervisor expects {"options": {…}}
    payload = json.dumps({"options": options}).encode("utf-8")
    req = urllib.request.Request(SUPERVISOR_API, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            if status == 200:
                log.info("Options posted to Supervisor successfully")
                return True
            else:
                log.error("Supervisor returned %s: %s", status, body)
                return False
    except Exception as e:
        log.error("Failed to post options to Supervisor: %s", e)
        return False


def migrate_options(options):
    """Apply FIELD_MAP to remap/drop fields. Returns the migrated options dict."""
    if not FIELD_MAP:
        return options

    migrated = {}
    for key, value in options.items():
        if key in FIELD_MAP:
            new_key = FIELD_MAP[key]
            if new_key is None:
                log.info("Dropping field: %s", key)
            else:
                log.info("Renaming field: %s → %s", key, new_key)
                migrated[new_key] = value
        else:
            migrated[key] = value

    return migrated


def run():
    """Main migration entry point."""
    stored = get_stored_version()
    log.info("Schema version: stored=%d, current=%d", stored, CURRENT_SCHEMA_VERSION)

    if stored >= CURRENT_SCHEMA_VERSION:
        log.info("No migration needed")
        return

    log.info("Migration required: %d → %d", stored, CURRENT_SCHEMA_VERSION)

    try:
        options = load_options()
    except Exception as e:
        log.error("Failed to load options: %s", e)
        return

    migrated = migrate_options(options)

    if migrated != options:
        log.info("Config changed — posting to Supervisor")
        if post_options(migrated):
            set_stored_version(CURRENT_SCHEMA_VERSION)
            log.info("Migration complete")
        else:
            log.error("Migration failed — Supervisor rejected the update")
    else:
        log.info("No field changes needed — updating schema version only")
        set_stored_version(CURRENT_SCHEMA_VERSION)


if __name__ == "__main__":
    run()
