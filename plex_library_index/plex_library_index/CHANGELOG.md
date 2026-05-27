# Changelog

## 1.2.5 — 2026-05-26

### Diagnostic / cleanup
- **Added explicit logging** to s6 service scripts so the add-on log shows progress messages even when Python crashes immediately. The `init-migrate` script now logs "starting", "done" / "FAILED with exit code N".
- **Removed deprecated `build.yaml`** — the Supervisor now requires build parameters inline in the Dockerfile. The OCI labels that were in build.yaml are now LABEL entries in the Dockerfile.
- **Added a Python import verification step** to the Dockerfile so the build fails clearly if `plexapi` or `aiohttp` doesn't install correctly.

## 1.2.4 — 2026-05-26

### Fixed
- **Add-on still failed to start in 1.2.3 despite the CR-stripping fix.** Root cause: the previous `sed 's/\r$//'` only strips `\r` immediately before `\n`, and may not handle every edge case (some Git configurations on Windows produce `\r` characters in unusual positions). Switched to `tr -d '\r'` which strips every CR byte in the file regardless of position.
- Added a build-time verification: after normalization, the s6 service scripts are checked for any remaining CR bytes. If any are found, the image build fails loudly with the offending file path, so the problem is visible in the build log instead of silently producing a broken image.

### Note for users
If the previous version (1.2.3) appeared to be installed but exhibited the same bashio startup error, this was due to Docker layer caching. To force a clean rebuild after this update: **Settings → Add-ons → Plex Library Index → ⋮ → Rebuild** (not just "Restart" or "Update").

## 1.2.3 — 2026-05-26

### Changed
- Restored the bashio shebang in s6 service scripts (`#!/usr/bin/with-contenv bashio`) to match the structural conventions of the user's other add-ons (WebURL SMS Login, Email WOL). The defensive CR-stripping step in the Dockerfile makes this safe against Windows CRLF issues regardless of upload path.
- Synced `io.hass.version` label to the current version.

## 1.2.2 — 2026-05-26

### Fixed
- **Add-on failed to start with `unable to exec bashio::log.info`.** Root cause: the s6 service scripts (`run`, `up`) were uploaded with CRLF line endings on Windows, breaking the bashio interpreter line. Two-part fix:
  - Removed the bashio dependency entirely — the scripts only ran a Python file, so plain `#!/command/with-contenv sh` is simpler and more robust.
  - Added a defensive `sed -i 's/\r$//'` step in the Dockerfile that strips any CR characters at build time, so the add-on can't be broken by CRLF uploads in the future.
- Updated `io.hass.version` label to track the current version.

### Upgrade note
Existing installations: pull the new commit and rebuild the add-on. No config changes needed.

## 1.2.1 — 2026-05-24

### Fixed
- Removed deprecated GitHub Actions build workflow that used the unmaintained `home-assistant/builder` action. HA's Supervisor builds the image itself when the add-on is installed, so CI-side builds were unnecessary.
- Lint workflow updated: uses current action versions, runs `python -m py_compile` and YAML validation as required checks, and runs `ruff` in lenient mode (real errors only, not style nits).

## 1.2.0 — 2026-05-24

### Added — Telegram bot integration
- **Self-contained Telegram client.** Outbound notifications for scan-complete / scan-failed, with the same "only on changes" semantics as the HA notify path. Use either, both, or neither.
- **Inbound command handling** (long-poll, no public HA exposure needed):
  - `/search <query>` — substring + fuzzy fallback search across movie and show titles
  - `/random` — random pick from the library
  - `/status` — index health and counts
  - `/help` / `/start` — show available commands
  - Plain text → library search
- **Per-user allow list** via `telegram_allowed_chat_ids` — everyone else is silently ignored.
- **Scope-limited handler.** Unknown slash commands and unmatched text are ignored, so a separate general-purpose Telegram bot (or HA `telegram_bot` integration) can coexist on different bot tokens.
- Telegram section in the Preferences page with connection status and a **send test message** button.
- New `POST /api/test-telegram` endpoint.
- Telegram fields surfaced in `/api/config-summary`.

### Config additions
- `telegram_enabled` (default `false`)
- `telegram_bot_token` (default empty)
- `telegram_allowed_chat_ids` (list of ints, default empty)
- `telegram_notify_on_success` (default `false`)
- `telegram_notify_on_error` (default `true`)
- `telegram_answer_commands` (default `true`)

### Internal
- Schema version bumped to v3; v2→v3 migration backfills the Telegram keys.
- Added in-memory library cache so Telegram queries don't re-read disk each time.
- New `telegram_bot.py` module — fully isolated, no HA dependencies.

## 1.1.0 — 2026-05-24

### Added
- **Built-in notifications** via Home Assistant's `notify.*` services. Fires on scan completion and/or failure, with optional "only notify when content changes" mode.
- **Preferences page** inside the ingress UI. Two sections:
  - Display preferences (sort, type, library, batch size, default filters, auto-refresh-on-open) — stored on the add-on, shared across users.
  - Read-only summary of server settings (with a "✓ found / ✗ not found" check against the configured notify service).
- **Test notification** button in the prefs page.
- `previous_counts` exposed in `/api/status` to surface deltas.
- `version` field in `/api/status`.

### New endpoints
- `GET /api/preferences` — read display preferences
- `PUT /api/preferences` — update display preferences
- `GET /api/notify-services` — list available HA `notify.*` services
- `GET /api/config-summary` — non-secret config view
- `POST /api/test-notify` — fire a test notification

### Config additions
- `notify_on_success` (default `false`)
- `notify_on_error` (default `true`)
- `notify_service` (default empty)
- `notify_only_on_changes` (default `false`)

### Internal
- Schema version bumped to v2; v1→v2 migration backfills notification keys.
- Added `homeassistant_api: true` so the add-on can call HA's REST API to fire notifications.

## 1.0.0 — 2026-05-24

Initial release.

- Plex library scanner running on the configured interval (default 24h).
- Ingress-served search UI: full-text search, library/type/quick filters, multiple sort modes, infinite scroll for large libraries.
- Per-row deep links to Plex web, IMDb, TMDb.
- Stats panel: top genres, decade breakdown, library breakdown.
- Duplicate detection.
- CSV export of the filtered view.
- `POST /api/refresh` endpoint for on-demand scans from automations.
- `GET /api/status` for monitoring scan state.
- Schema migration system with version tracking in `/data/.schema_version`.
- Gzipped JSON output (~91% smaller than uncompressed).
