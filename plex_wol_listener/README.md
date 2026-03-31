# Plex WoL Listener

A Home Assistant add-on that proxies Plex traffic and wakes your Plex server via Wake-on-LAN when a client connects. Includes Plex token validation, GeoIP blocking, flood detection, auto-sleep, HA sensors, dashboard toggles, and notifications.

## Tested on

- Home Assistant OS 17.1
- Home Assistant Core 2026.3.2
- Home Assistant Supervisor 2026.03.1
- Frontend 20260312.0
- Architecture: aarch64, amd64

## How it works

1. All Plex traffic is routed through this add-on (via port forwarding and Plex custom access URL)
2. When a Plex client connects and the server is asleep, the add-on sends a WoL magic packet
3. It waits for the server to come up, then transparently proxies the connection
4. If the server is already up, traffic is proxied immediately with no delay
5. Optionally validates Plex auth tokens before waking the server
6. Optionally puts the server to sleep after a configurable idle period

## Setup

### Router
- Change port forward from `external:32400 → <plex_server_ip>:32400` to `external:32400 → <home_assistant_ip>:32400`

### Plex server
- Settings → Network → Custom server access URLs: add `http://<home_assistant_ip>:32400`
- Save and restart Plex

### Add-on configuration
- `plex_server_ip`: IP of your Plex server (required)
- `target_mac`: MAC address of your Plex server for WoL (required)
- `broadcast`: Broadcast address for WoL (default: 0.0.0.0 — set to your subnet broadcast, e.g. 192.168.1.255)

### Dashboard card
A pre-built Lovelace card is included in `dashboard_card.yaml`. To add it:
1. Go to your HA dashboard → Edit Dashboard
2. Click "+ Add Card" → Manual
3. Paste the contents of `dashboard_card.yaml`

The card shows server status, connection count, unique IPs, last wake time, and toggle switches for GeoIP, quiet mode, and sleep trigger.

## Features

All features can be enabled/disabled individually from the add-on configuration page.

### User Tracking (`enable_user_tracking`)
Identifies which Plex user triggered a server wake by querying the Plex server's active sessions (`/status/sessions`) after it comes up. Requires `plex_admin_token` to be set. User info appears in logs, notifications, and the `sensor.plex_wol_last_wake_user` HA sensor. The query is made over HTTPS and the token never leaves your local network. Default: disabled.

### User Friendly Names (`user_friendly_names`)
Maps Plex usernames to display names for logs, notifications, and HA sensors. Format: `plexuser1:John,plexuser2:Jane`. Case-insensitive lookup — if no mapping exists, the original Plex username is shown. Requires user tracking to be enabled.

### Smart WoL / Burst Detection (`enable_smart_wol`)
When enabled, WoL is only sent when a burst of connections arrives within a short window, indicating someone actively opening the Plex app (cold open). A single background poll from an idle app (which happens every few minutes) will be ignored and the server stays asleep. Configurable:
- `smart_wol_burst_count`: number of connections required in the window to trigger WoL (default: 3)
- `smart_wol_burst_window`: time window in seconds (default: 15)

Once a burst triggers WoL, subsequent connections are allowed through while the server wakes. Burst state resets when the server comes up. Default: disabled.

### GeoIP Blocking (`enable_geoip`)
Blocks connections from countries not in the `allowed_countries` list. Uses ipapi.co over HTTPS for lookups with in-memory caching. Local/private IPs are always allowed. Controllable from the dashboard toggle. Default: enabled, US only.

### IP Allowlist (`enable_ip_allowlist`)
Only allows connections from specific IPs listed in `ip_allowlist` (comma-separated). Default: disabled.

### IP Blocklist (`enable_ip_blocklist`)
Drops connections from specific IPs listed in `ip_blocklist` (comma-separated). Blocklist is checked before allowlist and GeoIP, so blocked IPs are always rejected regardless of other settings. Use this to ban known scanners or abusive clients. Default: disabled.

### No-Wake List (`enable_nowake_list`)
IPs listed in `nowake_list` are proxied normally when the server is up, but if the server is down their connections are silently dropped instead of triggering WoL. The list is built from three sources: manual entries, auto-discovered relay IPs, and auto-learned infrastructure IPs (saved to `/data/learned_nowake.json`). Default: disabled.

### Auto-Discover Plex Relay IPs (`auto_discover_plex_relays`)
Queries plex.tv for your server's relay server IPs and adds them to the no-wake list. Runs at startup and periodically based on `relay_rediscover_hours` (default 6). Requires `plex_admin_token` and `enable_nowake_list` to both be set. If an auto-discovered IP is incorrectly flagged, add it to `allow_ip_plex_relay` to override. Default: disabled.

### Infrastructure Auto-Learning
When smart WoL is enabled and the server is down, IPs that send single probes (failing burst detection) are automatically learned as infrastructure and added to the no-wake list. Learned IPs persist across restarts in `/data/learned_nowake.json`.

### Allow Wake Override (`allow_ip_plex_relay`)
Comma-separated IPs or CIDR ranges (e.g. `34.0.0.0/8,198.27.160.147`) to exclude from the no-wake list. Overrides manual entries, auto-discovered relay IPs, and auto-learned IPs. Use to correct false positives.

### Log Dedup Cooldown (`log_dedup_cooldown_seconds`)
Controls how long repeated "WoL disabled, dropping connection" messages are suppressed per IP. After logging once, the same IP's drops are silent for this many seconds. Default: 300 (5 minutes).

### Flood Detection (`flood_threshold`, `flood_window_seconds`)
Sends an alert if more than `flood_threshold` connections arrive within `flood_window_seconds`. The Plex server's own IP and private/local IPs are excluded from flood counting to avoid false alerts from server self-connections and local traffic. Minimum threshold is 5. Default: 20 connections in 60 seconds.

### WoL Disable Toggle
The dashboard includes a `WoL Enabled` toggle (`input_boolean.plex_wol_enabled`). When turned off, the proxy continues to run and log connections, but WoL packets are not sent. Useful for debugging or when you want to manually control when the server wakes. Default: on.

### Sleep Trigger (`enable_sleep_trigger`)
Puts the Plex server to sleep via SSH after `sleep_idle_minutes` of no connections. Additionally, `max_awake_minutes` forces the server to sleep after a set duration regardless of active connections — this prevents clients with open Plex apps from keeping the server awake indefinitely. Both can be used independently or together. Controllable from the dashboard toggle. Requires:
1. OpenSSH Server enabled on Windows (install via PowerShell: `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`)
2. `server_ssh_user` set to your Windows username
3. SSH key authorized — the add-on generates a key on first start. Check the log for the public key and add it to `C:\Users\<you>\.ssh\authorized_keys` on your Plex server

Set `sleep_idle_minutes` to 0 to disable. Default: disabled.

### HA Sensors (`enable_ha_sensors`)
Exposes sensors in Home Assistant:
- `sensor.plex_wol_server_status` — up, waking, timeout, or unknown
- `sensor.plex_wol_last_wake` — timestamp of last WoL sent, with `user` attribute
- `sensor.plex_wol_last_wake_user` — Plex username of whoever last triggered a wake (requires user tracking)
- `sensor.plex_wol_connection_count` — total connections since add-on start
- `sensor.plex_wol_unique_ips_today` — unique IPs that connected today (resets daily)

### Dashboard Toggles
Feature toggles are managed via the health check HTTP endpoint on port 32401 and persisted to `/data/toggles.json`. Available toggles: `wol`, `geoip`, `quiet`, `sleep`. The dashboard card uses HA `rest_command` to call these endpoints. See `dashboard_card.yaml` for setup instructions including the required `rest_command` entries for `configuration.yaml`.
- `input_boolean.plex_wol_geoip` — enable/disable GeoIP blocking
- `input_boolean.plex_wol_quiet_mode` — enable/disable quiet mode
- `input_boolean.plex_wol_sleep_trigger` — enable/disable sleep trigger

The add-on polls these every 5 seconds and applies changes live. Default: enabled.

### Health Check (`enable_health_check`)
JSON status endpoint on `health_check_port` (default 32401). Returns server status, uptime, connection count, unique IPs, Plex server reachability, and last wake time. Can be used by HA's watchdog or external monitoring.

### File Logging (`enable_file_logging`)
Writes logs to `/data/plex_wol.log` with daily rotation. Old logs are deleted after `log_retention_days` days. Default: enabled, 14 days.

### Quiet Mode (`enable_quiet_mode`)
Suppresses routine "Server already up — proxying" and "Connection closed" log lines. WoL events, errors, timeouts, and alerts are always logged regardless. Controllable from the dashboard toggle. Default: disabled.

### Notifications
Sends persistent HA notifications (sidebar) on all alert events. If `notify_target` is set (e.g. `mobile_app_pixel_10_pro`), also sends mobile push notifications. Find your notify service in **Developer Tools → Services → notify**. Notification events include:
- Wake timeout (server failed to come up)
- Flood detection threshold exceeded
- Server put to sleep by idle trigger
- Server woken by user (if user tracking enabled)

### Auto-restart
If the proxy process crashes, the s6 finish script waits 5 seconds and restarts it automatically. No manual intervention needed.

### Graceful Shutdown
On SIGTERM (add-on stop/restart), all active proxy connections are cleanly closed before the process exits, preventing hung connections.

### Config Migration
On startup, a migration script checks for deprecated or renamed config fields and updates them automatically via the Supervisor API. Users can update the add-on without uninstalling — breaking config changes are handled transparently.

### WoL Toggle (`enable_wol`)
Master switch for Wake-on-LAN. When off, the proxy continues to run and forward traffic to the Plex server but never sends WoL packets. Useful for troubleshooting or running the add-on purely as a port redirect. The dashboard toggle must also be on for WoL to fire. Default: enabled.
