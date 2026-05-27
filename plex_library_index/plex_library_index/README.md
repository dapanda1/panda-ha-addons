# Plex Library Index — Home Assistant Add-on

A Home Assistant add-on that scans a Plex Media Server and serves a fast,
searchable library index inside Home Assistant. Lives in the sidebar via
ingress — authenticated through Home Assistant, works in the HA mobile app,
no exposed port.

> **Version 1.2.0** — adds Telegram bot integration (notifications + library search via chat).

## What it does

- Connects to your Plex server using the official Plex API (via [plexapi](https://github.com/pkkid/python-plexapi))
- Walks every movie and TV library, capturing:
  - Title, year, original title, content rating, runtime
  - Genres, directors, studio
  - Resolution (4K / 1080p / 720p / SD), video and audio codec, container, bitrate, file size
  - Watched state (per movie, per season)
  - External IDs: IMDb, TMDb, TVDB
  - Plex `ratingKey` and the server's `machineIdentifier` for deep links
- For TV shows: enumerates seasons with episode counts and viewed counts. Does **not** export per-episode data.
- Writes the result as `library.json.gz` (~130 KB for a 3000-item library)
- Serves a single-page search UI through HA ingress
- Re-scans on a configurable interval (default 24h)
- Sends Home Assistant notifications on scan completion and/or failure
- **Optional Telegram bot** — sends notifications and answers `/search` / `/random` / `/status` / plain-text queries from allowed chats
- Exposes API endpoints for triggering scans and reading status from automations

## Installation

### From this GitHub repository (recommended)

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add this repository's URL: `https://github.com/<your-username>/<this-repo>`
3. The repo appears in the store. Click into it, then click **Plex Library Index → Install**.
4. Open **Configuration** and fill in `plex_url` and `plex_token`. Save.
5. Click **Start**.
6. A **Plex Index** entry appears in the HA sidebar.

### Local development

1. Copy the `plex_library_index/` directory to `/addons/` on your HA host (via the Samba add-on or SSH).
2. **Settings → Add-ons → ⋮ → Check for updates**.
3. The add-on appears under **Local add-ons**. Install, configure, start.

## Configuration

Set in the add-on's **Configuration** tab in Home Assistant.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `plex_url` | URL | `http://homeassistant.local:32400` | Must be reachable from the add-on container |
| `plex_token` | password | `""` | Your X-Plex-Token. [How to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| `interval_hours` | int 1–168 | `24` | Time between automatic scans |
| `parallel_jobs` | int 1–16 | `4` | Concurrent show metadata fetches. 4–8 is the sweet spot on a Pi 4 |
| `gzip_output` | bool | `true` | Emit `library.json.gz`. Browser decodes natively |
| `log_level` | enum | `info` | `debug` / `info` / `warning` / `error` |
| `notify_on_success` | bool | `false` | Send a notification after each successful scan |
| `notify_on_error` | bool | `true` | Send a notification when a scan fails |
| `notify_service` | string | `""` | Name of an HA `notify.*` service. Leave blank to disable notifications |
| `notify_only_on_changes` | bool | `false` | Only send success notifications when counts differ from the prior scan |
| `telegram_enabled` | bool | `false` | Enable the built-in Telegram bot |
| `telegram_bot_token` | password | `""` | Bot token from [@BotFather](https://t.me/BotFather) |
| `telegram_allowed_chat_ids` | list[int] | `[]` | Numeric Telegram chat IDs allowed to use the bot. Empty = nobody |
| `telegram_notify_on_success` | bool | `false` | Send a Telegram message after each successful scan |
| `telegram_notify_on_error` | bool | `true` | Send a Telegram message when a scan fails |
| `telegram_answer_commands` | bool | `true` | Whether the bot responds to `/search`, `/random`, etc. |

The Preferences tab in the UI lists all `notify.*` services discovered on
your HA instance — copy the suffix (e.g. `mobile_app_pixel_8`) into
`notify_service`. The `notify.` prefix is optional.

## The UI

Two tabs:

### Browse

- Full-text search across title, original title, studio, library, year, genre, director, resolution, content rating
- Multi-token AND search ("nolan 2010" matches both terms)
- Filters: media type, library, recently added (30d), unwatched only, 4K only
- Sort by title, year ↑/↓, date added, rating, duration
- Infinite scroll (50-item batches by default; configurable in Preferences)
- Per-row links: open in Plex web, IMDb, TMDb
- Resolution and watched-state badges
- Stats panel: top 10 genres, decade breakdown, library breakdown
- Duplicate detection
- CSV export of the current filtered view
- Random pick button (picks one item from the filtered set, scrolls to it, flashes it)
- URL hash state — searches and filter combinations are bookmarkable
- "↻ refresh now" button to trigger an on-demand scan; the page polls status and auto-reloads when the scan completes

### Preferences

Three sections:

**Display preferences (stored on the add-on, shared across users):**
- Default sort
- Default media type
- Default library
- Items per scroll batch
- Whether to default to hide-watched / 4K-only / recently-added views
- Whether to trigger a scan automatically when the page opens

**Server settings (read-only mirror):**
- Plex URL, token status (✓ configured / ✗ not set)
- Scan interval, parallel jobs, gzip, log level

**Notifications:**
- Currently configured notify service, with a "✓ found / ✗ not found" check against the HA instance
- Notify on success / error / only-on-changes flags
- Full list of `notify.*` services available on this HA instance
- A **send test notification** button

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `/` | Focus search |
| `Esc` | Clear all filters |
| `r` | Random pick from current filtered set |

## Triggering scans externally

### From a Home Assistant automation

Add to `configuration.yaml`:

```yaml
rest_command:
  plex_index_refresh:
    url: "http://local-plex_library_index:8099/api/refresh"
    method: POST
    timeout: 10
```

Then automate:

```yaml
alias: "Plex Index — refresh on Plex server wake"
trigger:
  - platform: state
    entity_id: binary_sensor.plex_server_my_plex_box
    from: "off"
    to: "on"
    for: "00:00:30"
action:
  - service: rest_command.plex_index_refresh
```

Or a dashboard button:

```yaml
type: button
name: Rescan Plex Library
icon: mdi:refresh
tap_action:
  action: call-service
  service: rest_command.plex_index_refresh
```

## API endpoints

All served on the add-on's ingress path. Internal hostname for cross-container
calls is `local-plex_library_index` on port `8099`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Search page |
| GET | `/library.json` | Exported library (uncompressed) |
| GET | `/library.json.gz` | Exported library (gzipped, with `Content-Encoding: gzip`) |
| GET | `/api/status` | Scan state, last run, duration, counts, previous counts, errors, version |
| POST | `/api/refresh` | Start a scan. 409 if one is already running |
| GET | `/api/health` | `{ok: true}` |
| GET | `/api/preferences` | Read display preferences |
| PUT | `/api/preferences` | Update display preferences (JSON body) |
| GET | `/api/notify-services` | List of HA `notify.*` services on this instance |
| GET | `/api/config-summary` | Non-secret config view (no token included) |
| POST | `/api/test-notify` | Fire a one-off test notification |
| POST | `/api/test-telegram` | Fire a one-off test Telegram message |

### Example: status response

```json
{
  "scanning": false,
  "last_scan": "2026-05-24T10:00:00+00:00",
  "last_error": null,
  "last_duration_s": 47.3,
  "counts": {"movies": 2200, "shows": 800, "seasons": 3450, "episodes": 28000},
  "previous_counts": {"movies": 2198, "shows": 800, "seasons": 3448, "episodes": 27950},
  "next_scan_after": 1716724800.0,
  "version": "1.1.0"
}
```

## Performance

For a 3000-item library on a Pi 4 against a healthy Plex server, expect
30–90 seconds per full scan with `parallel_jobs: 4–8`. CPU is not the
bottleneck — Plex Media Server response time is. The output JSON is ~1.4 MB
uncompressed, ~130 KB gzipped (91% reduction).

The search UI uses infinite scroll with a pre-computed lowercase search blob
per item, so filtering 3000 items takes 5–10 ms.

## Data location

Everything persists in the add-on's `/data/` volume:

| Path | Contents |
|------|----------|
| `/data/options.json` | Add-on configuration (managed by Supervisor) |
| `/data/.schema_version` | Current config schema version |
| `/data/preferences.json` | Display preferences (set via UI) |
| `/data/state.json` | Last scan state |
| `/data/www/library.json[.gz]` | Exported library |
| `/data/www/index.html` | Search page (auto-installed on first start) |

## Schema migrations

The add-on tracks its config schema version in `/data/.schema_version`. On
startup, `migrate.py` runs as a oneshot s6 service before the main server,
applying any pending migrations and pushing the cleaned config back to the
Supervisor API. Current schema version: **2**.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ Home Assistant Sidebar                                         │
│  └─ "Plex Index" (ingress, HA auth)                            │
│       │                                                        │
│       ▼                                                        │
│  ┌─────────────────────────────────────────┐                   │
│  │ aiohttp server (port 8099, in-container)│                   │
│  │   ├─ GET  /             → index.html    │                   │
│  │   ├─ GET  /library.json.gz              │                   │
│  │   ├─ GET  /api/status                   │                   │
│  │   ├─ POST /api/refresh                  │                   │
│  │   ├─ GET/PUT /api/preferences           │                   │
│  │   ├─ GET  /api/notify-services          │──┐                │
│  │   ├─ GET  /api/config-summary           │  │                │
│  │   └─ POST /api/test-notify              │──┤                │
│  │                                         │  │                │
│  │ scheduled scan loop                     │  │                │
│  │   └─ exporter.run_export(opts) ─────────┼──┼─→ Plex API     │
│  │                                         │  │                │
│  │ writes /data/www/library.json.gz        │  │                │
│  │                                         │  │                │
│  │ notification dispatcher ────────────────┼──┴─→ HA REST API  │
│  │                                         │     /core/api/    │
│  └─────────────────────────────────────────┘                   │
└────────────────────────────────────────────────────────────────┘
```

## Telegram bot (optional)

Self-contained Telegram integration — sends notifications and answers
library queries from a whitelist of Telegram chats. Runs alongside the HA
notify path; use either, both, or neither.

### Quick setup

1. In Telegram, message **@BotFather**, run `/newbot`, get a bot token.
2. Message **@userinfobot** to find your numeric chat ID.
3. In the add-on Configuration tab:
   - `telegram_enabled: true`
   - `telegram_bot_token`: paste the token
   - `telegram_allowed_chat_ids`: `[12345678]` (your ID; add more for friends)
4. Save, restart. Open the Preferences tab → **send test telegram message**.

### Commands the bot understands

| Command | Response |
|---------|----------|
| `/search <query>` | Search the library |
| `/random` | Random pick from the library |
| `/status` | Index health and counts |
| `/help` or `/start` | Show command list |
| *plain text* | Searches the library by title (substring + fuzzy fallback) |

Anything else is silently ignored, so a separate general-purpose Telegram
bot (with a **different bot token**) can coexist without conflict.

### Why self-contained instead of HA's `telegram_bot` integration?

- Library data lives inside this add-on; routing through HA adds latency and complexity.
- Keeps the add-on usable on non-HA platforms (the Telegram module has no HA dependencies).
- HA's `telegram_bot` integration remains available for everything else — house controls, alerts from other automations, etc.

## Files in this directory

| File | Purpose |
|------|---------|
| `config.yaml` | Add-on manifest (read by Supervisor) |
| `build.yaml` | Multi-arch base image config |
| `Dockerfile` | Image build steps |
| `DOCS.md` | Rendered in HA's Documentation tab |
| `CHANGELOG.md` | Rendered in HA's Changelog tab |
| `README.md` | This file |
| `exporter.py` | Pure module — `run_export(opts)` returns the payload |
| `server.py` | aiohttp app + scheduler + notification dispatcher |
| `migrate.py` | Schema migrations |
| `telegram_bot.py` | Self-contained Telegram client and command handler |
| `www/index.html` | Search UI (Browse + Preferences) |
| `rootfs/etc/s6-overlay/...` | s6 service definitions |
| `images/` | Optional `icon.png` / `logo.png` |

## Portability

The add-on is structured to be useful outside Home Assistant if needed:

- `exporter.py` is an importable module with no HA dependencies. Run
  `from exporter import run_export; payload = run_export({"plex_url": ..., "plex_token": ..., "parallel_jobs": 4})` from anywhere.
- `www/index.html` is fully self-contained. It auto-detects whether
  `library.json.gz` or `library.json` is available in the same directory,
  so dropping the file pair on any static host (nginx, Caddy, GitHub Pages,
  HA `/config/www/`) works without changes.
- The notification path uses HA-specific APIs, but is opt-in via
  `notify_service`. Setting it to empty disables all HA-dependent behavior.

## Troubleshooting

**Add-on won't start** — check **Log** tab. Most often: missing `plex_token`
or unreachable `plex_url`.

**"plex_token not configured" in logs** — fill in `plex_token` under
Configuration and restart.

**Connection refused / timeout** — `plex_url` is unreachable from inside
the add-on. Most common cause is the Plex server being on a network the HA
container can't reach (VLAN, IoT isolation, firewall). Test from the HA
host with `curl http://<plex-ip>:32400/identity`.

**Notifications don't arrive** — open the **Preferences** tab. The
"Available notify services" list shows every `notify.*` service HA exposes.
The string in `notify_service` must match one of those (without the
`notify.` prefix, or with — either is accepted). The "✓ found / ✗ not
found" check next to the configured service name tells you immediately if
the name is wrong. If the list is empty, the add-on couldn't reach HA's
API at all — verify the add-on Configuration → "Show unused optional
settings" doesn't have `homeassistant_api` disabled, and restart.

**Initial scan never starts** — there's no library data on first run, so
the page shows "no library data yet — click ↻ refresh now". The first scan
also runs automatically; if it's not visible after ~2 minutes, check logs
for Plex API errors.

**Scan completes but UI shows old data** — the page polls `/api/status`
every 5s and auto-reloads when `last_scan` changes. If this stops working,
hard-refresh the browser (cache).

## License

MIT — see `LICENSE` at the repository root.
