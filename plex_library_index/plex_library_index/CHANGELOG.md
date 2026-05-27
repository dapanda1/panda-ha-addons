# Changelog

## 1.3.2 — 2026-05-27

### Changed — scheduling
- **Time-of-day scheduling.** Replaced "every N hours from startup" with explicit clock times. New options:
  - `scan_time` (default `"03:30"`) — when to run the daily scan, 24-hour HH:MM format
  - `scan_time_2` (optional) — second scan time per day
  - `interval_hours` is now a fallback used only when both `scan_time` and `scan_time_2` are blank
- **Capped quick-retry attempts.** When Plex is unreachable, the add-on now tries twice (after 5 min, then after 10 min) and then gives up until the next scheduled scan. Better fit for setups where the Plex server is offline most of the time — avoids retry loops chewing log space.
- Log line on startup announces the next scan time clearly, e.g.: `next scan at 2026-05-27T03:30:00-07:00 (daily at 03:30, in 32400s)`.

### Clarified
- **Icons.** Replaced placeholder `images/` folder with `ICONS.md` at the add-on root. Home Assistant looks for `icon.png` and `logo.png` directly in the add-on directory (next to `config.yaml`), not in an `images/` subfolder. Drop those two files alongside `config.yaml` if you want custom artwork; HA picks them up automatically. The add-on currently ships without artwork, so HA uses its generic placeholder.

### Internal
- Schema version bumped to v4; v3→v4 migration backfills `scan_time = "03:30"`, `scan_time_2 = ""`.

## 1.3.1 — 2026-05-27

### Changed
- Simplified Plex-unreachable retry schedule. Now retries first at 5 minutes, then every 10 minutes thereafter (was exponential backoff topping out at 60 min). Better fit for setups where the Plex server is typically offline and comes online intermittently.

## 1.3.0 — 2026-05-27

### Fixed
- **Preferences page button did nothing.** Cause: a Python-syntax `async def testNotify()` left in the JavaScript broke the inline script's parsing. Now correctly written as `async function`.
- **`VERSION` constant in `server.py` was stuck at 1.2.0.** Now tracks the package version.

### Added — resilience to Plex being offline
- **Quick-retry mode for connection failures.** When a scan fails because Plex is unreachable (host unreachable, connection refused, timeout, etc.), the add-on now retries on an exponential backoff (1 min → 2 → 5 → 10 → 15 → 30 → 60 min) until Plex comes back. Once Plex is reachable again, the schedule reverts to the normal `interval_hours`.
- **No notification spam during outages.** Connection-failure notifications fire only on the first failure of an outage, not every retry attempt. Other error types (auth failure, bad config) still notify normally.
- **New `/api/status` fields**: `retry_after`, `consecutive_failures`.

### Added — browser notifications
- New display preferences: "Browser notify on success" / "Browser notify on error".
- Uses the standard Web Notifications API — works on desktop browsers (Chrome, Firefox, Safari, Edge) and some mobile browsers. A permission-request button appears in Preferences the first time. Clicking a notification focuses the browser tab.
- Independent of HA notifications and Telegram — you can use one, two, or all three notification channels.

### Improved — configuration clarity
- Added `translations/en.yaml` with human-readable field labels and descriptions grouped by purpose: Plex connection, Scanning, Home Assistant notifications, Telegram bot. HA's add-on UI now shows full descriptive labels instead of raw field names.

## 1.2.9 — 2026-05-26

### Fixed (metadata)
- Replaced placeholder `noah` / `<your-username>` references throughout repository metadata with the actual repository owner (`dapanda1`) and repo (`panda-ha-addons`). Affected:
  - `plex_library_index/config.yaml` — `url` field
  - `plex_library_index/Dockerfile` — `org.opencontainers.image.source` label
  - `repository.yaml` — `url` and `maintainer` fields
  - `README.md` — install instructions
  - `plex_library_index/README.md` — install instructions
  - `LICENSE` — copyright holder
- No code or runtime behavior changes.

## 1.2.8 — 2026-05-26

### Critical fix — this is the real root cause
- **Removed the `init-migrate` s6 oneshot.** Per the s6 documentation: oneshot `up` scripts are parsed by `s6-rc-compile` using execline-style parsing, NOT as POSIX shell scripts. The shebang line is ignored entirely. This explains why every previous attempt to fix the script via shebang adjustments failed: the shebang was never honored to begin with. The script's first line was being interpreted as an argv string, producing errors like `unable to exec bashio::log.info` or `unable to exec TS=$(date`.
- **Folded schema migration into the longrun `server/run` script.** Longrun scripts DO honor their shebang (they run under `s6-supervise`, not `s6-rc-compile`). So `#!/usr/bin/with-contenv bashio` works correctly there. The new `server/run` runs `migrate.py` first, then execs `server.py`.
- Net result: the add-on has one s6 service (longrun `server`) instead of two (oneshot `init-migrate` + longrun `server`). Same behavior, simpler structure, actually works.

### Build cache fix from 1.2.7 retained
- The `BUILD_VERSION` cache-bust in the Dockerfile is kept so future updates won't be hidden by Docker layer caching.

## 1.2.7 — 2026-05-26

### Critical fix
- **Forced cache invalidation on version bumps.** The Supervisor passes `BUILD_VERSION` as a build-arg, but the previous Dockerfile didn't reference it, so Docker reused cached layers from prior versions. This meant version bumps would *appear* to install but the actual image used pre-update file content. The Dockerfile now references `${BUILD_VERSION}` in a RUN echo before the COPY layers, forcing every layer downstream to rebuild on each version change.

### Diagnostic
- Build output now prints `[build] verified <file> is CR-free` and `[build] shebang: <line>` for each s6 service script during build. This makes it possible to verify from the *build log* exactly what shebang the running container will see.
- Build also prints the version it's building, e.g. `Building Plex Library Index version 1.2.7`.

### Note for the previous failures
v1.2.4 and v1.2.5 were almost certainly running stale cached content rather than the newly committed code. The build appeared to succeed in 5 seconds because Docker reused every layer — including the layer with the (broken) s6 scripts. This release breaks that cache.

## 1.2.6 — 2026-05-26

### Added
- **Timestamps on all log lines.** `migrate.py` now uses Python's logging module with ISO-8601 timestamp prefixes, matching `server.py`'s format. The s6 service scripts wrap each bashio log line with an explicit `[YYYY-MM-DDTHH:MM:SS]` prefix so every entry has a date/time stamp regardless of source.
- Note: s6-overlay's own startup messages (`s6-rc: info: service ...`) are produced by s6 itself and remain untimestamped. Those are upstream and cannot be changed without rebuilding the base image.

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
