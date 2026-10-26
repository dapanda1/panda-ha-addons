# Plex WoL Listener

A Home Assistant add-on that proxies Plex traffic and wakes your Plex server via Wake-on-LAN when a client connects. Includes offline GeoIP blocking, IP/CIDR allow and block lists, flood detection, auto-sleep, wake history, HA sensors, dashboard toggles, and notifications.

## Tested on

- Home Assistant OS 17.1
- Home Assistant Core 2026.3.2
- Home Assistant Supervisor 2026.03.1
- Frontend 20260312.0
- Architecture: aarch64, amd64

## How it works

1. All Plex traffic is routed through this add-on (via port forwarding and Plex custom access URL)
2. When a Plex client connects and the server is asleep, the add-on sends a WoL magic packet
3. It waits until Plex itself is answering (not just the PC), then transparently proxies the connection
4. If the server is already up, traffic is proxied immediately with no delay
5. Optionally puts the server to sleep after a configurable idle period

## Setup

### Router
- Change port forward from `external:32400 → <plex_server_ip>:32400` to `external:32400 → <home_assistant_ip>:32400`

### Plex server
- Settings → Network → Custom server access URLs: add `http://<home_assistant_ip>:32400`
- Save and restart Plex

### Add-on configuration
- `plex_server_ip`: IP of your Plex server (required). Must not be the Home Assistant host's own IP when both ports are the same — the add-on refuses that setting, since the proxy would connect to itself.
- `target_mac`: MAC address of your Plex server for WoL (required)
- `broadcast`: Broadcast address for WoL (default: 255.255.255.255 — set to your subnet broadcast, e.g. 192.168.1.255, if WoL doesn't reach the server)

### Dashboard card
A pre-built Lovelace card is included in `dashboard_card.yaml`. To add it:
1. Go to your HA dashboard → Edit Dashboard
2. Click "+ Add Card" → Manual
3. Paste the contents of `dashboard_card.yaml`

The card shows server status, connection count, unique IPs, last wake time, wake history, and toggle buttons for WoL, GeoIP, quiet mode, and sleep trigger.

## Features

All features can be enabled/disabled individually from the add-on configuration page.

### Wake Readiness Check
After a wake, the add-on waits until Plex answers its `/identity` endpoint, not just until port 32400 opens, so clients aren't handed to a server that's still starting. If the wake times out, it tells two failures apart:
- **Wake failed** — nothing answered at all (the PC didn't wake)
- **Plex not running** — the PC is awake and refusing connections on the Plex port, so Plex Media Server isn't running

If the port opens but Plex never answers `/identity` before the timeout, the connection is proxied anyway and a warning is logged.

All clients waiting on the same wake share one wait: one readiness check, one notification, and one history entry per wake.

### Wake History
Every wake attempt is recorded with its time, the IP that triggered it, whether a WoL packet was sent, the outcome (`ready`, `unverified`, `plex_not_running` or `timeout`), how many seconds Plex took to be ready, and the Plex user (when user tracking is on). The last 100 wakes are kept in `/data/wake_history.json` and are available from:
- `GET /history` on the health check port (newest first)
- `sensor.plex_wol_wake_history` — the latest outcome, with the five most recent wakes and today's wake count as attributes
- `last_wake_outcome` in `GET /status`

### User Tracking (`enable_user_tracking`)
Identifies which Plex user triggered a server wake by querying the Plex server's active sessions (`/status/sessions`) after it comes up. Requires `plex_admin_token` to be set. User info appears in logs, notifications, wake history, and the `sensor.plex_wol_last_wake_user` HA sensor. The query is made over HTTPS and the token never leaves your local network. Default: disabled.

### User Friendly Names (`user_friendly_names`)
Maps Plex usernames to display names for logs, notifications, and HA sensors. Format: `plexuser1:John,plexuser2:Jane`. Case-insensitive lookup — if no mapping exists, the original Plex username is shown. Requires user tracking to be enabled.

### Smart WoL / Burst Detection (`enable_smart_wol`)
When enabled, WoL is only sent when a burst of connections from one IP arrives within a short window, indicating someone actively opening the Plex app (cold open). A single background poll from an idle app (which happens every few minutes) will be ignored and the server stays asleep. Connections from different IPs don't add up to a burst. Configurable:
- `smart_wol_burst_count`: number of connections from one IP required in the window to trigger WoL (default: 3)
- `smart_wol_burst_window`: time window in seconds (default: 15)

Once a burst triggers WoL, subsequent connections are allowed through while the server wakes. Burst state resets when the server comes up. Default: disabled.

### GeoIP Blocking (`enable_geoip`)
Blocks connections from countries not in the `allowed_countries` list. Lookups use the DB-IP Lite country database bundled into the add-on, so they're instant, work offline, and have no rate limits. Local, private, and CGNAT/Tailscale (100.64.0.0/10) addresses are always allowed, as are the rare public IPs with no country on record. Each blocked IP is logged once a day. Controllable from the dashboard toggle. Default: enabled, US only.

The database is pinned in the `Dockerfile` and updated with the add-on.

### IP Allowlist (`enable_ip_allowlist`)
Only allows connections from the IPs or CIDR ranges listed in `ip_allowlist` (comma-separated, e.g. `203.0.113.50,198.51.100.0/24`). Default: disabled.

### IP Blocklist (`enable_ip_blocklist`)
Drops connections from the IPs or CIDR ranges listed in `ip_blocklist` (comma-separated). Blocklist is checked before allowlist and GeoIP, so blocked IPs are always rejected regardless of other settings. Use this to ban known scanners or abusive clients. Default: disabled.

### No-Wake List (`enable_nowake_list`)
IPs and CIDR ranges listed in `nowake_list` are proxied normally when the server is up, but if the server is down their connections are silently dropped instead of triggering WoL. The list is built from three sources: manual entries, auto-discovered relay IPs, and auto-learned infrastructure IPs (saved to `/data/learned_nowake.json`). Default: disabled.

### Auto-Discover Plex Relay IPs (`auto_discover_plex_relays`)
Queries plex.tv for your server's relay server IPs and adds them to the no-wake list. Runs at startup and periodically based on `relay_rediscover_hours` (default 6). Requires `plex_admin_token` and `enable_nowake_list` to both be set. If an auto-discovered IP is incorrectly flagged, add it to `allow_ip_plex_relay` to override. Default: disabled.

### Infrastructure Auto-Learning
When smart WoL is enabled and the server is down, a public IP that sends a single probe and nothing else within the burst window is automatically learned as infrastructure and added to the no-wake list. An IP that connects again within the window — the start of a real app launch — is never learned. Learned IPs persist across restarts in `/data/learned_nowake.json` and are visible in the `learned_nowake_ips` config field (auto-managed, do not edit directly).

### Allow Wake Override (`allow_ip_plex_relay`)
Comma-separated IPs or CIDR ranges (e.g. `34.0.0.0/8,198.27.160.147`) to exclude from the no-wake list. Overrides manual entries, auto-discovered relay IPs, and auto-learned IPs. Adding an IP here also permanently removes it from the learned file. Use to correct false positives.

### Log Dedup Cooldown (`log_dedup_cooldown_seconds`)
Controls how long repeated "WoL disabled, dropping connection" messages are suppressed per IP. After logging once, the same IP's drops are silent for this many seconds. Default: 300 (5 minutes).

### Flood Detection (`flood_threshold`, `flood_window_seconds`)
Sends an alert if more than `flood_threshold` connections arrive within `flood_window_seconds`. The Plex server's own IP and private/local IPs are excluded from flood counting to avoid false alerts from server self-connections and local traffic. Minimum threshold is 5. Default: 20 connections in 60 seconds.

### Sleep Trigger (`enable_sleep_trigger`)
Puts the Plex server to sleep via SSH after `sleep_idle_minutes` of no connections. Additionally, `max_awake_minutes` forces the server to sleep after a set duration regardless of active connections — this prevents clients with open Plex apps from keeping the server awake indefinitely. Both can be used independently or together. Controllable from the dashboard toggle. Requires:
1. OpenSSH Server enabled on Windows (install via PowerShell: `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`)
2. `server_ssh_user` set to your Windows username
3. SSH key authorized — the add-on generates a key on first start. Check the log for the public key and add it to `C:\Users\<you>\.ssh\authorized_keys` on your Plex server

Set `sleep_idle_minutes` to 0 to disable. Default: disabled.

### HA Sensors (`enable_ha_sensors`)
Exposes sensors in Home Assistant:
- `sensor.plex_wol_server_status` — `up`, `waking`, `down` (WoL disabled), `timeout`, `plex_not_running`, or `unknown`
- `sensor.plex_wol_last_wake` — timestamp of last WoL sent, with `user` attribute
- `sensor.plex_wol_last_wake_user` — Plex username of whoever last triggered a wake (requires user tracking)
- `sensor.plex_wol_connection_count` — total connections since add-on start
- `sensor.plex_wol_unique_ips_today` — unique IPs that connected today (resets daily)
- `sensor.plex_wol_wake_history` — latest wake outcome, with recent wakes as attributes

Sensor updates are sent in the background so they never delay a Plex connection; rapid updates to the same sensor are combined.

### Dashboard Toggles
Feature toggles are managed via the health check HTTP endpoint on port 32401 and persisted to `/data/toggles.json`. Available toggles: `wol`, `geoip`, `quiet`, `sleep`. The dashboard card uses HA `rest_command` to call these endpoints. See `dashboard_card.yaml` for setup instructions including the required `rest_command` entries for `configuration.yaml`.
- `POST /toggle/<name>` — flip a toggle
- `POST /toggle/<name>/on` or `/off` — set it explicitly (useful in automations)

Each toggle starts from its config option. A dashboard change persists across restarts until you change that option in the add-on config, at which point the config value takes over. Requests sent from a web browser (those carrying an `Origin` header) are refused, so a web page can't flip your toggles; `rest_command` is unaffected.

### Health Check (`enable_health_check`)
JSON status endpoint on `health_check_port` (default 32401). `GET /status` returns server status, uptime, connection count, unique IPs, Plex server reachability, last wake time and outcome, and toggle states. `GET /history` returns the wake history. Can be used by HA's watchdog or external monitoring.

### File Logging (`enable_file_logging`)
Writes logs to `/data/plex_wol.log` with daily rotation. Old logs are deleted after `log_retention_days` days. Default: enabled, 14 days.

### Quiet Mode (`enable_quiet_mode`)
Suppresses routine "Server already up — proxying" and "Connection closed" log lines. WoL events, errors, timeouts, and alerts are always logged regardless. Controllable from the dashboard toggle. Default: disabled.

### Notifications
Sends persistent HA notifications (sidebar) and optional mobile push (via `notify_target`). Each notification type can be individually toggled from the config page:
- `notify_wake_timeout` — server failed to respond after WoL, or the PC woke but Plex isn't running (default: on)
- `notify_flood` — connection flood threshold exceeded (default: on)
- `notify_server_slept` — idle/max awake timer triggered sleep (default: on)
- `notify_server_woken` — user identified after wake (default: on)
- `notify_geoip_block` — connection blocked by country, once per IP per day (default: off, can be noisy)

### Auto-restart
If the proxy process crashes, the s6 finish script waits 5 seconds and restarts it automatically. No manual intervention needed.

### Graceful Shutdown
On SIGTERM (add-on stop/restart), all active proxy connections are cleanly closed before the process exits, preventing hung connections.

### Config Migration
On startup, a migration script checks for deprecated or renamed config fields and updates them automatically via the Supervisor API. Users can update the add-on without uninstalling — breaking config changes are handled transparently. Your saved options and everything in `/data` (toggles, learned IPs, wake history, SSH key, logs) are kept across updates.

### WoL Toggle (`enable_wol`)
Master switch for Wake-on-LAN. When off, the proxy continues to run and forward traffic to the Plex server but never sends WoL packets. Useful for troubleshooting or running the add-on purely as a port redirect. The dashboard toggle must also be on for WoL to fire. Default: enabled.

## Development

Run the tests from the add-on directory:

```
python3 -m unittest discover -s tests
```

GeoIP tests against the real database run when `GEOIP_TEST_DB` points at a `dbip-country-ipv4.mmdb` and the `maxminddb` package is installed; otherwise they're skipped.

## Credits

[IP Geolocation by DB-IP](https://db-ip.com) — the bundled country database is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
