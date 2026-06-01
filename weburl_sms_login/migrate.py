#!/usr/bin/env python3
"""
WebURL SMS Login — Config Migration

Handles schema migrations when config.yaml options/schema change between
versions. On startup, compares the stored schema version to CURRENT_SCHEMA_VERSION.
If outdated, remaps/drops fields and POSTs the updated config to the Supervisor API.

To add a migration:
  1. Bump CURRENT_SCHEMA_VERSION
  2. Add old→new mappings to FIELD_MAP (value=None to drop a field)
  3. Add a migration function to MIGRATIONS dict keyed by target version
  4. Update options/schema in config.yaml
  5. Bump version in config.yaml
"""

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  [migrate] %(message)s",
)
log = logging.getLogger("migrate")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 1

OPTIONS_PATH = Path("/data/options.json")
SCHEMA_VERSION_PATH = Path("/data/.schema_version")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
SUPERVISOR_OPTIONS_URL = "http://supervisor/addons/self/options"

# ---------------------------------------------------------------------------
# Field map — old name → new name (None = drop)
#
# This is a cumulative map. Each migration function below handles its own
# version-specific renames. FIELD_MAP is provided as a quick reference and
# used by the generic remap helper.
# ---------------------------------------------------------------------------

FIELD_MAP = {
    # Example for future use:
    # "old_field_name": "new_field_name",
    # "removed_field": None,
}

# ---------------------------------------------------------------------------
# Migration functions — one per schema version bump
#
# Each function receives the current config dict and returns the modified
# config dict. They run in order from stored_version+1 to CURRENT_SCHEMA_VERSION.
# ---------------------------------------------------------------------------


def migrate_to_v1(cfg: dict) -> dict:
    """Initial schema — no changes needed, just sets the baseline."""
    return cfg


# Register migrations: key = target version, value = function
MIGRATIONS = {
    1: migrate_to_v1,
    # Future example:
    # 2: migrate_to_v2,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_stored_version() -> int:
    """Read the stored schema version. Returns 0 if no version file exists."""
    if SCHEMA_VERSION_PATH.exists():
        try:
            return int(SCHEMA_VERSION_PATH.read_text().strip())
        except (ValueError, OSError):
            pass
    return 0


def set_stored_version(version: int):
    """Write the schema version to disk."""
    SCHEMA_VERSION_PATH.write_text(str(version))
    log.info(f"Schema version set to {version}")


def load_options() -> dict:
    """Load current options.json."""
    if not OPTIONS_PATH.exists():
        log.warning("options.json not found — nothing to migrate")
        return {}
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def apply_field_map(cfg: dict, field_map: dict) -> dict:
    """
    Generic field remapper. For each entry in field_map:
      - If value is None, drop the key
      - If value is a string, rename the key (preserving the value)
    Only acts on keys that exist in cfg.
    """
    for old_key, new_key in field_map.items():
        if old_key in cfg:
            if new_key is None:
                del cfg[old_key]
                log.info(f"  Dropped field: {old_key}")
            elif old_key != new_key:
                cfg[new_key] = cfg.pop(old_key)
                log.info(f"  Renamed: {old_key} → {new_key}")
    return cfg


def post_options(cfg: dict) -> bool:
    """POST updated config to the Supervisor API."""
    if not SUPERVISOR_TOKEN:
        log.error("No SUPERVISOR_TOKEN — cannot update add-on options")
        return False

    payload = json.dumps({"options": cfg}).encode()
    req = urllib.request.Request(
        SUPERVISOR_OPTIONS_URL,
        data=payload,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {SUPERVISOR_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("result") == "ok":
                log.info("Options updated via Supervisor API")
                return True
            else:
                log.error(f"Supervisor API returned: {result}")
                return False
    except Exception as e:
        log.error(f"Failed to POST options: {e}")
        return False


# ---------------------------------------------------------------------------
# Main migration logic
# ---------------------------------------------------------------------------


def run_migrations():
    stored = get_stored_version()

    if stored >= CURRENT_SCHEMA_VERSION:
        log.info(f"Schema version {stored} is current (target: {CURRENT_SCHEMA_VERSION}) — no migration needed")
        return True

    log.info(f"Migration needed: {stored} → {CURRENT_SCHEMA_VERSION}")

    cfg = load_options()
    if not cfg:
        # No options to migrate — just set the version
        set_stored_version(CURRENT_SCHEMA_VERSION)
        return True

    # Run each migration step in order
    for target_version in range(stored + 1, CURRENT_SCHEMA_VERSION + 1):
        migration_fn = MIGRATIONS.get(target_version)
        if migration_fn:
            log.info(f"Running migration to v{target_version}: {migration_fn.__name__}")
            try:
                cfg = migration_fn(cfg)
            except Exception as e:
                log.error(f"Migration to v{target_version} failed: {e}")
                return False
        else:
            log.warning(f"No migration function for v{target_version} — skipping")

    # POST the updated config if we actually changed something
    if stored > 0:
        if not post_options(cfg):
            log.error("Failed to persist migrated config — will retry on next start")
            return False

    set_stored_version(CURRENT_SCHEMA_VERSION)
    log.info("Migration complete")
    return True


if __name__ == "__main__":
    success = run_migrations()
    sys.exit(0 if success else 1)
