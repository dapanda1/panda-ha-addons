# Plex Library Index

A searchable index of your Plex movies and TV shows, served directly inside
Home Assistant. Lives in the sidebar via ingress — no exposed port, HA login
required, works in the HA companion app.

## Configuration

### `plex_url` (required)

URL to the Plex Media Server. Example: `http://192.168.1.50:32400`. Must be
reachable from inside the add-on container.

### `plex_token` (required)

Plex authentication token. Get yours here:
https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/

### `interval_hours`

How often the library is automatically rescanned. Default `24`. Range 1–168.

### `parallel_jobs`

Number of concurrent threads used to fetch show season data. Default `4`.
Range 1–16. Sweet spot for a Pi 4 is 4–8.

### `gzip_output`

When `true` (default), the output is written as `library.json.gz`.

### `log_level`

`debug`, `info` (default), `warning`, `error`.

### `notify_service`

Name of a Home Assistant `notify.*` service to use for notifications.
Examples: `mobile_app_pixel_8`, `persistent_notification`, `telegram_bot`.
The `notify.` prefix is optional — both `mobile_app_pixel_8` and
`notify.mobile_app_pixel_8` work.

Leave blank to disable notifications entirely.

The Preferences page in the UI lists all `notify.*` services found on your
HA instance — copy the suffix into this field.

### `notify_on_success`

When `true`, fires a notification after each successful scan with counts and
duration. Default `false`.

### `notify_on_error`

When `true`, fires a notification when a scan fails. Default `true`.

### `notify_only_on_changes`

When `true`, success notifications only fire if the movie/show/season/episode
counts differ from the previous scan. Useful if you only want to hear about
new additions. Default `false`.

## Preferences (in-UI)

The **Preferences** tab in the sidebar UI has two sections:

### Display preferences (stored in the add-on)

- Default sort order
- Default media type filter
- Default library
- Items rendered per scroll batch
- Whether to default to unwatched / 4K-only / recently-added views
- Whether to trigger a scan automatically when the page is opened

These persist across restarts and apply to anyone using the add-on.

### Server settings (read-only)

Mirror of the add-on's Configuration tab so you can verify state at a glance.
Includes a check that confirms whether the configured `notify_service`
actually exists on this HA instance.

The **Test notification** button fires a one-off notification using the
currently configured service.

## Triggering scans

### From the UI

Click **↻ refresh now** in the chip row. The page polls the status endpoint
every 5 seconds and reloads when the scan completes.

### From a Home Assistant automation

The add-on exposes `POST /api/refresh` on its internal port. Add to
`configuration.yaml`:

```yaml
rest_command:
  plex_index_refresh:
    url: "http://local-plex_library_index:8099/api/refresh"
    method: POST
    timeout: 10
```

Example automation — refresh when the Plex server wakes up (requires HA's
official Plex integration):

```yaml
alias: "Plex Index — refresh on Plex wake"
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

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Search page |
| GET | `/library.json[.gz]` | Exported library |
| GET | `/api/status` | Scan state, last run, counts, errors |
| POST | `/api/refresh` | Start a scan (409 if already running) |
| GET | `/api/health` | `{ok: true}` |
| GET | `/api/preferences` | Read display preferences |
| PUT | `/api/preferences` | Update display preferences |
| GET | `/api/notify-services` | List available HA `notify.*` services |
| GET | `/api/config-summary` | Non-secret config view |
| POST | `/api/test-notify` | Fire a test notification |

## Data location

- `/data/options.json` — add-on config (managed by Supervisor)
- `/data/.schema_version` — current config schema version
- `/data/preferences.json` — display preferences (set via UI)
- `/data/state.json` — last scan state (persisted across restarts)
- `/data/www/library.json[.gz]` — exported library
- `/data/www/index.html` — search page

## Performance

For a 3000-item library on a Pi 4 against a healthy PMS, expect 30–90s per
full scan with `parallel_jobs: 4–8`. Output is ~1.4 MB JSON, ~130 KB
gzipped. The bottleneck is PMS response time, not the Pi.

## Troubleshooting

**"plex_token not configured" in logs** — fill in `plex_token` under
Configuration and restart.

**Connection refused** — `plex_url` is unreachable from the add-on. Verify
from a different HA add-on, or test from the HA host itself:
`curl http://<plex-ip>:32400/identity`.

**Notification button shows error / no notification received** — open the
Preferences tab and check the "Available notify services" list. The service
name in `notify_service` must match exactly (without the `notify.` prefix
or with it; either is accepted). If the list is empty, the add-on can't
reach HA's API — verify `homeassistant_api: true` is set in `config.yaml`
and restart.

**Scan completes but the UI shows old data** — try a hard refresh (cache).
The page auto-reloads when `/api/status` reports a new `last_scan`
timestamp, so this should be rare.

## Telegram integration (optional)

The add-on includes a self-contained Telegram client. It runs alongside the
HA notify path — you can use either, both, or neither.

### What it does

**Outbound:**
- Sends `scan complete` / `scan failed` messages to allowed Telegram chats.
- Same "only on changes" semantics as the HA notify path.

**Inbound (long-poll, no public HA exposure needed):**
- `/search <query>` — substring + fuzzy search across all movie and show titles
- `/random` — pick a random title
- `/status` — index health, counts, last scan time
- `/help` / `/start` — show commands
- Any plain text → searches the library

### Setup

1. Open Telegram, message **@BotFather**, send `/newbot`. Follow the prompts.
   You'll receive a token like `123456789:ABCdef...`.
2. Find your numeric chat ID: message **@userinfobot** in Telegram, it
   replies with your ID (e.g. `12345678`).
3. In this add-on's Configuration:
   - `telegram_enabled`: `true`
   - `telegram_bot_token`: paste the token
   - `telegram_allowed_chat_ids`: list of allowed numeric IDs, one per line
     (or, in YAML/JSON: `[12345678, 87654321]`)
4. Save and restart the add-on.
5. Open the Preferences tab in the UI and click **send test telegram message**.

### Telegram config keys

| Field | Default | Notes |
|-------|---------|-------|
| `telegram_enabled` | `false` | Master switch |
| `telegram_bot_token` | `""` | From @BotFather |
| `telegram_allowed_chat_ids` | `[]` | Empty list = nobody can use the bot |
| `telegram_notify_on_success` | `false` | Send "scan complete" messages |
| `telegram_notify_on_error` | `true` | Send "scan failed" messages |
| `telegram_answer_commands` | `true` | Whether the bot answers `/search` etc. |

### Coexistence with other Telegram bots

The add-on only handles `/search`, `/random`, `/status`, `/help`, `/start`,
and plain text. Other slash commands are silently ignored, so a separate
general-purpose Telegram bot (using a **different bot token**) can coexist
peacefully. Each bot only sees messages addressed to its own username.

### Security notes

- The bot rejects any message from a chat ID not in
  `telegram_allowed_chat_ids`. Rejected messages get no reply.
- The bot token is treated as a secret — it's masked in the UI and never
  echoed back from `/api/config-summary`.
- Telegram is a third-party service. Message contents (queries, results)
  pass through Telegram's servers.

### Troubleshooting

**"⚠ enabled but not connected" in Preferences** — bot token is invalid or
the add-on can't reach `api.telegram.org`. Check logs.

**Bot doesn't reply to your messages** — your chat ID isn't in
`telegram_allowed_chat_ids`. The add-on log shows
`rejected message from chat_id=N`.

**Bot can't find titles you know exist** — search uses substring matching
first, then fuzzy fallback at 62% similarity. Try fewer / shorter tokens.
