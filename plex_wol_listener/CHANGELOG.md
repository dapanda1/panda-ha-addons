# Changelog

## 5.4.5
- Replaced `DashboardToggles` (broken fake state entities) with `ToggleManager` — file-persisted toggles in `/data/toggles.json`, no HA helper entities needed
- Rewrote health check HTTP server with full routing: `GET /status`, `GET /toggles`, `POST /toggle/<n>`, `POST /toggle/<n>/on|off`. CORS headers included.
- Dashboard card now uses `rest_command` calls to toggle features via the health check port (32401). Requires adding `rest_command` entries to HA `configuration.yaml` — see `dashboard_card.yaml` for setup instructions.
- Removed `enable_dashboard_toggles` config option — toggles are always available via the health check endpoint
- Toggle states persist across restarts in `/data/toggles.json`

## 5.4.4
- Rewrote dashboard toggles to use real `input_boolean` helpers via HA's config API instead of fake state entities. Toggles now have unique IDs, persist across HA restarts, and respond properly to UI clicks. Delete any old "no unique ID" entities after upgrading.

## 5.4.3
- Burst detection and auto-learning now run before the WoL enabled check. You can disable WoL, leave the server asleep, and the add-on will still learn infrastructure IPs from single probes. Turn WoL back on when ready — learned IPs are already on the no-wake list.

## 5.4.2
- Removed server-up infrastructure learning (`InfrastructureLearner`) — unreliable since session API can't match source IPs through the proxy, causing real users to be incorrectly flagged
- Removed `infra_learn_threshold` and `infra_learn_window_hours` config options
- Server-down auto-learning (single probe = infrastructure) remains — this is reliable
- Migration schema bumped to v3 to clean up removed fields

## 5.4.1
- Infrastructure auto-learning now uses a time window (`infra_learn_window_hours`, default 24). Hits older than the window are discarded. An IP must reach the threshold within the window to be learned, preventing infrequent legitimate users from being incorrectly flagged.

## 5.4
- Removed unused `urllib.parse` import
- Centralized version string into `VERSION` constant
- GeoIP cache now expires entries after 24 hours
- Confirmed admin token is never logged
- Added `.gitignore` for GitHub publishing
- Notification target now configurable via `notify_target` — no longer hardcoded
- Switched GeoIP to ipapi.co (HTTPS)
- Persistent auto-learning: IPs that fail burst detection are saved to `/data/learned_nowake.json` and loaded on every restart
- Session-based learning: when user tracking is on, public IPs that connect repeatedly without appearing in active sessions are auto-learned as infrastructure
- Periodic relay re-discovery (`relay_rediscover_hours`): re-queries plex.tv on a timer, not just at startup. Default 6 hours.
- `allow_ip_plex_relay` now supports CIDR notation (e.g. `34.0.0.0/8`) in addition to single IPs
- New config option `infra_learn_threshold`: number of non-session connections before an IP is auto-learned. Default 5.

## 5.3.7
- Switched GeoIP provider from ip-api.com (HTTP) to ipapi.co (HTTPS). All external requests now use encrypted connections. Free tier, no API key, 1000 lookups/day.

## 5.3.6
- Fixed plex.tv relay auto-discovery returning HTTP 400 — added required `X-Plex-Client-Identifier` and `X-Plex-Product` headers to the API request

## 5.3.5
- Config dependency corrections are now persisted to the Supervisor API so the HA UI reflects the actual state of toggles after auto-enable/disable

## 5.3.4
- Added config dependency enforcement at startup: features that depend on other settings are auto-enabled or auto-disabled with a log message explaining why. Covers auto-discover + no-wake list, auto-discover + admin token, user tracking + admin token, sleep trigger + SSH user.

## 5.3.3
- Fixed optional config fields showing as required (red asterisk) — `plex_admin_token`, `ip_allowlist`, `ip_blocklist`, `nowake_list`, `allow_ip_plex_relay`, `server_ssh_user`, `user_friendly_names` now accept empty values
- Startup log now shows the full active no-wake IP list (manual + auto-discovered - excluded)
- Updated `allow_ip_plex_relay` description to explain that auto-discovered IPs are in-memory only and re-discovered on every restart

## 5.3.2
- Renamed `nowake_exclude` to `allow_ip_plex_relay` for clarity. Migration handles the rename automatically.

## 5.3.1
- Added no-wake exclusion list (`allow_ip_plex_relay`): comma-separated IPs to remove from the no-wake list, overriding both manual entries and auto-discovered relay IPs. Use to correct false flags from auto-discovery.

## 5.3
- Added configurable log dedup cooldown (`log_dedup_cooldown_seconds`): controls how long repeated "WoL disabled" messages are suppressed per IP. Default 300 seconds (5 min).
- Added auto-discover Plex relay IPs (`auto_discover_plex_relays`): on startup, queries plex.tv for your server's relay IPs and adds them to the no-wake list automatically. Requires admin token and no-wake list enabled.

## 5.2.2
- No-wake list connections are now completely silent when the server is down — no log output at all
- "WoL disabled, dropping connection" messages are now deduplicated per IP — logged once then suppressed for 5 minutes to reduce log noise from retrying clients

## 5.2.1
- Fixed: when WoL is disabled (config or dashboard), connections are now dropped immediately instead of waiting 120 seconds for a server that will never wake

## 5.2
- Added `enable_wol` config toggle: master switch for WoL. When off, the proxy still runs and forwards traffic but never sends WoL packets. WoL only fires if both the config toggle and dashboard toggle are on. Log shows which one disabled it.

## 5.1
- Added config migration system (`migrate.py`): runs on every startup, compares stored schema version to current, renames/drops deprecated config fields automatically, and POSTs cleaned config to the Supervisor API. Users can update without uninstalling. `enable_token_validation` mapped as dropped from v5.0.

## 5.0
- Removed Plex token validation (cannot inspect TLS-encrypted traffic from modern Plex clients)
- Replaced with session-based user tracking (`enable_user_tracking`): queries the Plex server's `/status/sessions` endpoint over HTTPS after a wake to identify who connected
- New config option `plex_admin_token`: required for user tracking. Token sent via HTTPS header, never leaves LAN
- User tracking is disabled if `enable_user_tracking` is off or `plex_admin_token` is empty
- Friendly names (`user_friendly_names`) now work with session-based tracking
- Wake notifications now include which user triggered the wake

## 4.7
- Added user friendly names (`user_friendly_names`): map Plex usernames to display names shown in logs, notifications, and sensors. Format: `plexuser1:John,plexuser2:Jane`. Case-insensitive. Falls back to Plex username if no mapping exists.

## 4.6
- Plex username now shown in log lines when token validation is enabled (e.g. `(user: JohnDoe) Server down — sending WoL`)
- Wake failure notifications include the Plex username that triggered the wake attempt
- New HA sensor `sensor.plex_wol_last_wake_user` — shows who last woke the server
- `sensor.plex_wol_last_wake` now includes a `user` attribute
- Health check JSON includes `last_wake_user`
- Dashboard card updated with Last Wake User sensor
- Token validator now caches username alongside token validity

## 4.5
- Added no-wake list (`enable_nowake_list`, `nowake_list`): listed IPs are proxied normally when the server is up, but never trigger WoL when it's down. Use for plex.tv health-check IPs and similar services.
- Tightened all configuration descriptions to be concise and consistent

## 4.4
- Flood detection now excludes the Plex server's own IP and all private/local IPs from counting, preventing false alerts from server self-connections and local device traffic
- Sleep trigger now ignores connections from the Plex server's own IP, so server maintenance tasks don't reset the idle timer
- Raised default flood threshold from 10 to 20 — normal Plex app cold opens generate 10-15+ connections which triggered false alerts at the old default

## 4.3
- Added IP blocklist (`enable_ip_blocklist`, `ip_blocklist`): explicitly block specific IP addresses. Blocklist is checked before allowlist and GeoIP, so blocked IPs are always dropped regardless of other settings.

## 4.2
- Added smart WoL / burst detection (`enable_smart_wol`): only sends WoL when a burst of connections is detected within a short window, indicating an actual app cold open rather than a single background poll from an idle app. Configurable `smart_wol_burst_count` (default 3) and `smart_wol_burst_window` (default 15 seconds). Off by default.
- Background polls from idle apps are logged and dropped when smart WoL is enabled and the server is asleep, preventing unnecessary wake-ups.
- Burst state resets automatically when the server comes up.

## 4.1
- Added WoL disable toggle (`input_boolean.plex_wol_enabled`): disables WoL packets while proxy and logging continue to run. Added to dashboard card.
- Added `max_awake_minutes`: forces server to sleep after a set duration regardless of active connections. Prevents clients with open apps from keeping the server awake indefinitely. Set to 0 to disable (default).
- Sleep trigger now supports both idle timeout and max awake timeout independently or together.

## 4.0
- Added Plex token validation (`enable_token_validation`): validates X-Plex-Token against plex.tv before waking or proxying. Rejects unauthenticated connections with 401. Caches valid tokens for 1 hour
- Added HA dashboard card (Lovelace YAML included in `dashboard_card.yaml`)
- Added dashboard toggles: live control of GeoIP, quiet mode, and sleep trigger via `input_boolean` entities — no restart required
- Added connection history sensor (`sensor.plex_wol_unique_ips_today`): tracks unique IPs per day, resets daily, IP list in attributes
- Added auto-restart via s6 `finish` script: 5-second delay then automatic restart on crash
- Added graceful shutdown: handles SIGTERM, cleanly closes all active proxy connections
- Added connection tracking for active proxy sessions
- Health check endpoint now includes unique IPs today in JSON response
- Refactored HA API calls into shared helpers

## 3.3
- Added toggleable quiet mode (`enable_quiet_mode`): suppresses routine "Server already up" and "Connection closed" log lines while keeping WoL events, errors, timeouts, and alerts visible

## 3.2
- Health check endpoint now returns JSON with server status, uptime, connection count, last wake time, Plex server reachability, and timestamp

## 3.1
- Added minimum floor of 5 for flood_threshold (prevents accidental alert spam)

## 3.0
- Added GeoIP blocking with configurable allowed countries (default: US only)
- Added IP allowlist for restricting access to specific IPs
- Added sleep trigger: puts Plex server to sleep via SSH after configurable idle period
- Added HA sensors: server status, last wake time, connection count
- Added health check HTTP endpoint for monitoring
- Added file-based logging with daily rotation and configurable retention
- Added README with full setup instructions
- All new features are individually toggleable from the add-on configuration
- Added openssh-client to container for sleep trigger SSH support
- SSH key auto-generated on first start

## 2.4
- Added Home Assistant notifications for wake timeout failures
- Added flood detection alerts (configurable threshold and window)
- Added persistent notifications (HA sidebar) and mobile push notifications
- Added `homeassistant_api: true` for HA API access
- Added configurable `flood_threshold` and `flood_window_seconds` options
- Startup log now shows HA API token status

## 2.3
- Fixed s6-overlay v3 compatibility by adding `"init": false` to config

## 2.2
- Rebuilt with proper s6-overlay v3 service structure
- Added `rootfs/etc/s6-overlay/s6-rc.d/` directory layout
- Registered service via `user/contents.d` bundle
- Replaced `CMD` with s6 longrun service

## 2.1
- Attempted s6 service fix using `/etc/services.d/` (v2 pattern, did not work on HA OS 17.1)

## 2.0
- Full rewrite of listener as a TCP proxy
- Connections are now held open, proxied transparently to the real Plex server
- Added WoL with server wake polling before proxying
- Added `plex_server_ip`, `plex_server_port`, `listen_port`, `wake_timeout_seconds` options
- Removed `wakeonlan` pip dependency, replaced with pure Python WoL implementation
- Switched from `python:3.11-slim` to HA base image with `apk add python3`
- Removed deprecated architectures (`armv7`, `armhf`, `i386`)
- Multi-threaded: each client connection handled in its own thread

## 1.2
- Original version
- Listened on port 32400, accepted connections, sent WoL, immediately closed connection
- Used `wakeonlan` pip package
- Based on `python:3.11-slim` Docker image
- No proxying — Plex clients received dead connections
