"""
Plex WoL Proxy Add-on v4.0 for Home Assistant.

Features (all toggleable):
- TCP proxy: transparently relays Plex traffic
- WoL: wakes sleeping Plex server on incoming connection
- Plex token validation: rejects unauthenticated connections before waking server
- GeoIP blocking: drops connections from non-allowed countries
- IP allowlist: only allow specific IPs
- Flood detection: alerts on connection spikes
- Sleep trigger: puts server to sleep after idle period via SSH
- HA sensors: server status, connection count, last wake, unique IPs today
- HA dashboard toggles: input_boolean entities for GeoIP/quiet/sleep
- Health check: JSON status endpoint
- File logging: daily rotation with configurable retention
- HA notifications: push + persistent on failures and alerts
- Graceful shutdown: cleanly closes active connections on SIGTERM
- Auto-restart: s6 finish script with delay
"""

import collections
import ipaddress
import json
import logging
import logging.handlers
import os
import signal
import selectors
import socket
import ssl
import subprocess
import threading
import time
import urllib.request

OPTIONS_PATH = "/data/options.json"
VERSION = "5.4.9"
PROXY_BUF = 65536
CONNECT_POLL_INTERVAL = 2
SSH_KEY_PATH = "/data/plex_wol_key"
LOG_FILE_PATH = "/data/plex_wol.log"

SUPERVISOR_URL = "http://supervisor"
HA_API = f"{SUPERVISOR_URL}/core/api"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
file_logger = None


def setup_file_logging(enabled, retention_days):
    global file_logger
    if not enabled:
        file_logger = None
        return
    file_logger = logging.getLogger("plex_wol_file")
    file_logger.setLevel(logging.INFO)
    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE_PATH, when="midnight", backupCount=retention_days, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    file_logger.addHandler(handler)


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if file_logger:
        file_logger.info(line)


# ---------------------------------------------------------------------------
# Log deduplication
# ---------------------------------------------------------------------------
class LogDedup:
    """Suppresses repeated log messages from the same key for a cooldown period."""

    def __init__(self, cooldown=300):
        self.cooldown = cooldown  # seconds
        self.last_logged = {}  # key -> timestamp
        self.lock = threading.Lock()

    def set_cooldown(self, cooldown):
        self.cooldown = cooldown

    def should_log(self, key):
        """Returns True if this key hasn't been logged within the cooldown period."""
        now = time.time()
        with self.lock:
            last = self.last_logged.get(key, 0)
            if now - last >= self.cooldown:
                self.last_logged[key] = now
                return True
            return False


# Global instance — cooldown updated from config in main()
wol_disabled_dedup = LogDedup()


# ---------------------------------------------------------------------------
# Home Assistant API helpers
# ---------------------------------------------------------------------------
def ha_api_post(endpoint, payload):
    """POST to HA API. Returns True on success."""
    if not SUPERVISOR_TOKEN:
        return False
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{HA_API}{endpoint}",
            data=data, headers=headers, method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log(f"WARNING: HA API POST {endpoint} failed: {e}")
        return False


def supervisor_request(path, payload=None, timeout=10):
    """GET (no payload) or POST JSON to the Supervisor API. Returns the response's `data`."""
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("no SUPERVISOR_TOKEN")
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{SUPERVISOR_URL}{path}", data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    resp = urllib.request.urlopen(req, timeout=timeout)
    body = resp.read().decode()
    return (json.loads(body) if body else {}).get("data") or {}


def update_addon_options(changes):
    """Apply `changes` on top of the add-on's current saved options.

    Reads the live options from /addons/self/info rather than /data/options.json,
    which is only written at container start — posting that back would silently
    undo any config the user has saved since. Returns True if anything changed.
    """
    current = supervisor_request("/addons/self/info").get("options", {})
    if all(current.get(k) == v for k, v in changes.items()):
        return False
    current.update(changes)
    supervisor_request("/addons/self/options", {"options": current}, timeout=30)
    return True


# ---------------------------------------------------------------------------
# Background worker for HA / Supervisor calls
# ---------------------------------------------------------------------------
class BackgroundWorker:
    """Runs HA / Supervisor calls one at a time, off the connection path.

    A job submitted with a key replaces any pending job with the same key (only
    the latest state matters) and runs at most once per `min_interval` seconds.
    Keyless jobs, like notifications, always run, in order, ahead of keyed ones.
    """

    def __init__(self):
        self.cond = threading.Condition()
        self.queue = collections.deque()
        self.keyed = {}  # key -> (fn, min_interval)
        self.last_run = {}  # key -> monotonic time
        self.started = False

    def start(self):
        with self.cond:
            if self.started:
                return
            self.started = True
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn, key=None, min_interval=0):
        with self.cond:
            if key is None:
                self.queue.append(fn)
            else:
                self.keyed[key] = (fn, min_interval)
            self.cond.notify()

    def _next_job(self):
        """Pop the next runnable job. Returns (job, None) or (None, seconds until one is due)."""
        if self.queue:
            return self.queue.popleft(), None
        now = time.monotonic()
        wait = None
        for key, (fn, min_interval) in self.keyed.items():
            due = self.last_run.get(key, float("-inf")) + min_interval
            if due <= now:
                del self.keyed[key]
                self.last_run[key] = now
                return fn, None
            wait = due - now if wait is None else min(wait, due - now)
        return None, wait

    def _run(self):
        while True:
            with self.cond:
                job, wait = self._next_job()
                while job is None:
                    self.cond.wait(wait)
                    job, wait = self._next_job()
            try:
                job()
            except Exception as e:
                log(f"WARNING: background task failed: {e}")


ha_worker = BackgroundWorker()  # started in main()


NOTIFY_TARGET = ""  # set from config in main()
NOTIFY_ENABLED = {}  # category -> bool, set from config in main()


def ha_notify(title, message, category=None):
    """Send notification via HA persistent notification + mobile app.
    If category is set, checks NOTIFY_ENABLED before sending."""
    if category and not NOTIFY_ENABLED.get(category, True):
        return
    ha_api_post("/services/persistent_notification/create", {
        "title": title,
        "message": message,
        "notification_id": f"plex_wol_{time.time_ns()}",
    })
    if NOTIFY_TARGET:
        ha_api_post(f"/services/notify/{NOTIFY_TARGET}", {
            "title": title,
            "message": message,
        })


def notify(title, message, category=None):
    """Queue a notification without blocking the caller."""
    if category and not NOTIFY_ENABLED.get(category, True):
        return
    ha_worker.submit(lambda: ha_notify(title, message, category))


def post_state(entity_id, state, attributes=None, min_interval=0):
    """Queue a sensor state update. Rapid updates to one entity collapse into the latest."""
    payload = {"state": state, "attributes": attributes or {}}
    ha_worker.submit(lambda: ha_api_post(f"/states/{entity_id}", payload),
                     key=f"state:{entity_id}", min_interval=min_interval)


# ---------------------------------------------------------------------------
# HA Sensors
# ---------------------------------------------------------------------------
class HASensors:
    def __init__(self, enabled):
        self.enabled = enabled
        self.lock = threading.Lock()
        self.connection_count = 0
        self.last_wake_time = None
        self.last_wake_user = None
        self.server_status = "unknown"
        self.start_time = time.time()

    def _post_state(self, entity_id, state, attributes=None, min_interval=0):
        if not self.enabled:
            return
        post_state(entity_id, state, attributes, min_interval)

    def set_server_status(self, status):
        self.server_status = status
        self._post_state("sensor.plex_wol_server_status", status, {
            "friendly_name": "Plex Server Status",
            "icon": "mdi:server",
        })

    def set_last_wake(self, plex_user=None):
        self.last_wake_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_wake_user = plex_user
        self._post_state("sensor.plex_wol_last_wake", self.last_wake_time, {
            "friendly_name": "Plex Server Last Wake",
            "icon": "mdi:alarm",
            "user": plex_user or "unknown",
        })
        self._post_state("sensor.plex_wol_last_wake_user", plex_user or "unknown", {
            "friendly_name": "Plex WoL Last Wake User",
            "icon": "mdi:account",
            "wake_time": self.last_wake_time,
        })

    def increment_connections(self):
        with self.lock:
            self.connection_count += 1
            count = self.connection_count
        self._post_state("sensor.plex_wol_connection_count", str(count), {
            "friendly_name": "Plex WoL Connection Count",
            "icon": "mdi:counter",
        }, min_interval=5)

    def get_uptime(self):
        seconds = int(time.time() - self.start_time)
        days, r = divmod(seconds, 86400)
        hours, r = divmod(r, 3600)
        minutes, secs = divmod(r, 60)
        return f"{days}d {hours}h {minutes}m {secs}s", seconds

    def publish_all(self):
        self.set_server_status(self.server_status)
        self._post_state("sensor.plex_wol_last_wake", self.last_wake_time or "never", {
            "friendly_name": "Plex Server Last Wake",
            "icon": "mdi:alarm",
            "user": self.last_wake_user or "unknown",
        })
        self._post_state("sensor.plex_wol_last_wake_user", self.last_wake_user or "unknown", {
            "friendly_name": "Plex WoL Last Wake User",
            "icon": "mdi:account",
        })
        self._post_state("sensor.plex_wol_connection_count", str(self.connection_count), {
            "friendly_name": "Plex WoL Connection Count",
            "icon": "mdi:counter",
        })


# ---------------------------------------------------------------------------
# Connection history (unique IPs per day)
# ---------------------------------------------------------------------------
class ConnectionHistory:
    def __init__(self, enabled):
        self.enabled = enabled
        self.lock = threading.Lock()
        self.today = time.strftime("%Y-%m-%d")
        self.unique_ips = set()

    def record(self, ip_str):
        if not self.enabled:
            return
        with self.lock:
            current_day = time.strftime("%Y-%m-%d")
            if current_day != self.today:
                self.today = current_day
                self.unique_ips = set()
            is_new = ip_str not in self.unique_ips
            self.unique_ips.add(ip_str)
            data = {"date": self.today, "ips": sorted(self.unique_ips)} if is_new else None
        if data:
            # Throttled: the attribute carries the whole day's IP list
            post_state("sensor.plex_wol_unique_ips_today", str(len(data["ips"])), {
                "friendly_name": "Plex WoL Unique IPs Today",
                "icon": "mdi:ip-network",
                "date": data["date"],
                "ips": data["ips"],
            }, min_interval=30)

    def get_data(self):
        with self.lock:
            return {"date": self.today, "count": len(self.unique_ips), "ips": sorted(self.unique_ips)}


# ---------------------------------------------------------------------------
# HA Dashboard toggles
# ---------------------------------------------------------------------------
class ToggleManager:
    """Manages feature toggles with file persistence. Controlled via health check HTTP endpoint.

    Each toggle starts from its config value. A dashboard change persists across
    restarts until the matching config value is changed, at which point the new
    config value wins. The file stores the config value each toggle was seeded
    from so that change can be detected.
    """

    TOGGLES_FILE = "/data/toggles.json"

    def __init__(self, initial_wol, initial_geoip, initial_quiet, initial_sleep):
        self.lock = threading.Lock()
        self.config = {
            "wol": bool(initial_wol),
            "geoip": bool(initial_geoip),
            "quiet": bool(initial_quiet),
            "sleep": bool(initial_sleep),
        }
        self.state = dict(self.config)
        self._load()
        self._save()
        log(f"Toggles: wol={'ON' if self.state['wol'] else 'OFF'} "
            f"geoip={'ON' if self.state['geoip'] else 'OFF'} "
            f"quiet={'ON' if self.state['quiet'] else 'OFF'} "
            f"sleep={'ON' if self.state['sleep'] else 'OFF'}")

    def _load(self):
        """Apply saved toggle states whose config value hasn't changed since they were saved."""
        try:
            with open(self.TOGGLES_FILE, "r") as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log(f"Toggles: failed to load {self.TOGGLES_FILE}: {e}")
            return

        if "state" in saved:
            saved_state = saved.get("state", {})
            saved_config = saved.get("config", {})
        else:
            # Pre-5.5 flat format has no record of the config it came from, so
            # keep the dashboard choices; config changes apply from now on.
            saved_state, saved_config = saved, dict(self.config)

        for key in self.state:
            if key not in saved_state:
                continue
            if saved_config.get(key) == self.config[key]:
                self.state[key] = bool(saved_state[key])
            else:
                log(f"Toggles: {key} config changed to {'ON' if self.config[key] else 'OFF'} "
                    f"— overriding saved dashboard toggle")
            if self.state[key] != self.config[key]:
                log(f"Toggles: {key} is {'ON' if self.state[key] else 'OFF'} from the dashboard "
                    f"(config says {'ON' if self.config[key] else 'OFF'})")

    def _save(self):
        try:
            with open(self.TOGGLES_FILE, "w") as f:
                json.dump({"state": self.state, "config": self.config}, f, indent=2)
        except Exception as e:
            log(f"Toggles: failed to save: {e}")

    def toggle(self, name):
        """Toggle a feature. Returns (success, new_value)."""
        with self.lock:
            if name not in self.state:
                return False, None
            self.state[name] = not self.state[name]
            new_val = self.state[name]
            self._save()
        log(f"Toggle: {name} changed to {'ON' if new_val else 'OFF'}")
        return True, new_val

    def set(self, name, value):
        """Set a toggle to a specific value. Returns (success, new_value)."""
        with self.lock:
            if name not in self.state:
                return False, None
            self.state[name] = bool(value)
            new_val = self.state[name]
            self._save()
        log(f"Toggle: {name} set to {'ON' if new_val else 'OFF'}")
        return True, new_val

    def get_all(self):
        with self.lock:
            return dict(self.state)

    def get_wol(self):
        with self.lock:
            return self.state["wol"]

    def get_geoip(self):
        with self.lock:
            return self.state["geoip"]

    def get_quiet(self):
        with self.lock:
            return self.state["quiet"]

    def get_sleep(self):
        with self.lock:
            return self.state["sleep"]


# ---------------------------------------------------------------------------
# Plex session-based user tracking
# ---------------------------------------------------------------------------
class PlexSessionTracker:
    """Queries the Plex server's /status/sessions endpoint to identify active users."""

    def __init__(self, enabled, server_ip, server_port, admin_token):
        self.enabled = enabled and bool(admin_token)
        self.server_ip = server_ip
        self.server_port = server_port
        self.admin_token = admin_token

        if enabled and not admin_token:
            log("User tracking: disabled (no plex_admin_token set)")
        elif self.enabled:
            log("User tracking: enabled")

        # SSL context that skips cert verification (plex.direct cert won't match local IP)
        self._ssl_ctx = None
        try:
            import ssl
            self._ssl_ctx = ssl.create_default_context()
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            pass

    def get_active_users(self):
        """Query Plex server for active sessions. Returns list of usernames."""
        if not self.enabled:
            return []
        try:
            url = f"https://{self.server_ip}:{self.server_port}/status/sessions"
            req = urllib.request.Request(url, headers={
                "X-Plex-Token": self.admin_token,
                "Accept": "application/json",
            })
            resp = urllib.request.urlopen(req, timeout=5, context=self._ssl_ctx)
            data = json.loads(resp.read().decode())
            users = set()
            mc = data.get("MediaContainer", {})
            for item in mc.get("Metadata", []):
                user = item.get("User", {})
                title = user.get("title") or user.get("username")
                if title:
                    users.add(title)
            return sorted(users)
        except Exception as e:
            log(f"User tracking: failed to query sessions: {e}")
            return []

    def get_wake_user(self, delay=10):
        """Wait briefly then check who connected after a wake. Returns username or None."""
        if not self.enabled:
            return None
        time.sleep(delay)
        users = self.get_active_users()
        if users:
            return users[0]  # most likely the person who triggered the wake
        return None


# ---------------------------------------------------------------------------
# GeoIP
# ---------------------------------------------------------------------------
GEOIP_DB_PATH = "/usr/share/dbip/dbip-country-ipv4.mmdb"


class GeoIPChecker:
    """Country lookups against the bundled DB-IP Lite database: local, instant, no rate limits."""

    def __init__(self, enabled, allowed_countries_str, toggle_manager=None, db_path=GEOIP_DB_PATH):
        self.base_enabled = enabled
        self.allowed = set()
        self.toggle_manager = toggle_manager
        if allowed_countries_str:
            self.allowed = {c.strip().upper() for c in allowed_countries_str.split(",") if c.strip()}
        # Each blocked or unknown IP is logged (and notified) once a day, not per connection
        self.reported = LogDedup(cooldown=86400)
        self.reader = None
        try:
            import maxminddb
            self.reader = maxminddb.open_database(db_path)
            built = time.strftime("%Y-%m-%d", time.gmtime(self.reader.metadata().build_epoch))
            log(f"GeoIP: using DB-IP Lite database built {built} (IP Geolocation by DB-IP, https://db-ip.com)")
        except Exception as e:
            log(f"GeoIP: ERROR — could not open {db_path}: {e}. GeoIP blocking is inactive.")

    @property
    def enabled(self):
        if self.toggle_manager:
            return self.toggle_manager.get_geoip()
        return self.base_enabled

    def country(self, ip_str):
        """ISO country code for a public IP, or None if unknown."""
        if self.reader is None:
            return None
        try:
            record = self.reader.get(ip_str)
        except Exception:
            return None
        return ((record or {}).get("country_code") or "").upper() or None

    def is_allowed(self, ip_str):
        if not self.enabled or self.reader is None:
            return True
        try:
            if not ipaddress.ip_address(ip_str).is_global:
                return True  # LAN, loopback, CGNAT/Tailscale (100.64/10), etc.
        except ValueError:
            return True

        country = self.country(ip_str)
        if country is None:
            if self.reported.should_log(ip_str):
                log(f"GeoIP: no country on record for {ip_str} — allowing")
            return True
        if country in self.allowed:
            return True

        if self.reported.should_log(ip_str):
            log(f"GeoIP BLOCKED: {ip_str} is from {country} (allowed: {sorted(self.allowed)})")
            notify("Plex WoL: GeoIP blocked",
                   f"Connection from {ip_str} blocked — country {country} not in allowed list.",
                   "geoip_block")
        return False


# ---------------------------------------------------------------------------
# IP / CIDR matching
# ---------------------------------------------------------------------------
class IPMatcher:
    """Matches IPs against a comma-separated list of addresses and CIDR ranges
    (e.g. "203.0.113.5,198.51.100.0/24"). Supports `ip in matcher`."""

    def __init__(self, entries_str, name="list"):
        self.ips = set()
        self.networks = []
        invalid = []
        for entry in (entries_str or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if "/" in entry:
                    self.networks.append(ipaddress.ip_network(entry, strict=False))
                else:
                    self.ips.add(str(ipaddress.ip_address(entry)))
            except ValueError:
                invalid.append(entry)
        if invalid:
            log(f"WARNING: {name}: ignoring invalid IP/CIDR entries: {', '.join(invalid)}")

    def __contains__(self, ip_str):
        if ip_str in self.ips:
            return True
        if not self.networks:
            return False
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(addr in net for net in self.networks)

    def entries(self):
        return sorted(self.ips) + [str(net) for net in self.networks]


# ---------------------------------------------------------------------------
# IP Allowlist
# ---------------------------------------------------------------------------
class IPAllowlist:
    def __init__(self, enabled, allowlist_str):
        self.enabled = enabled
        self.allowed = IPMatcher(allowlist_str, "ip_allowlist")

    def is_allowed(self, ip_str):
        if not self.enabled:
            return True
        allowed = ip_str in self.allowed
        if not allowed:
            log(f"IP ALLOWLIST BLOCKED: {ip_str} not in allowlist")
        return allowed


# ---------------------------------------------------------------------------
# IP Blocklist
# ---------------------------------------------------------------------------
class IPBlocklist:
    def __init__(self, enabled, blocklist_str):
        self.enabled = enabled
        self.blocked = IPMatcher(blocklist_str, "ip_blocklist")

    def is_blocked(self, ip_str):
        if not self.enabled:
            return False
        if ip_str in self.blocked:
            log(f"IP BLOCKLIST BLOCKED: {ip_str}")
            return True
        return False


# ---------------------------------------------------------------------------
# No-wake list
# ---------------------------------------------------------------------------
class NoWakeList:
    """IPs that are proxied when the server is up but never trigger WoL when it's down."""

    LEARNED_FILE = "/data/learned_nowake.json"

    def __init__(self, enabled, nowake_str, auto_discover=False, admin_token="",
                 exclude_str="", rediscover_hours=6):
        self.enabled = enabled
        self.ips = set()  # learned and auto-discovered single IPs
        self.manual = IPMatcher(nowake_str, "nowake_list")  # IPs and CIDR ranges
        self.exclude = IPMatcher(exclude_str, "allow_ip_plex_relay")  # overrides everything above
        self.admin_token = admin_token
        self.auto_discover = auto_discover
        self.lock = threading.Lock()

        # Load previously learned IPs
        self._load_learned()

        if auto_discover and admin_token:
            self._discover_relay_ips()

        # Apply exclusions
        self._apply_exclusions()

        # Sync learned IPs to config UI on startup
        self._startup_sync()

        # Start periodic re-discovery
        if auto_discover and admin_token and rediscover_hours > 0:
            def _periodic():
                while True:
                    time.sleep(rediscover_hours * 3600)
                    log(f"No-wake: periodic re-discovery running")
                    self._discover_relay_ips()
                    self._apply_exclusions()
                    log(f"No-wake active: {self.get_active_ips()}")
            t = threading.Thread(target=_periodic, daemon=True)
            t.start()

    def _is_excluded(self, ip_str):
        """Check if an IP is in the exclusion list (exact or CIDR)."""
        return ip_str in self.exclude

    def _apply_exclusions(self):
        """Remove excluded IPs from the no-wake list and from the learned file."""
        with self.lock:
            removed = {ip for ip in self.ips if self._is_excluded(ip)}
            if not removed:
                return
            self.ips -= removed
            log(f"Allow wake override: removed {sorted(removed)} from no-wake list")

            # Also clean excluded IPs from the learned file
            try:
                learned = self._read_learned_file()
                cleaned = learned - removed
                if cleaned != learned:
                    self._save_learned(cleaned)
                    log(f"Allow wake override: cleaned {len(learned - cleaned)} excluded IPs from learned file")
            except Exception as e:
                log(f"Allow wake override: failed to clean learned file: {e}")

    def _load_learned(self):
        """Load learned no-wake IPs from persistent file."""
        try:
            with open(self.LEARNED_FILE, "r") as f:
                data = json.load(f)
                learned = set(data.get("ips", []))
                if learned:
                    self.ips.update(learned)
                    log(f"No-wake learned: loaded {len(learned)} IPs from {self.LEARNED_FILE}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"No-wake learned: failed to load {self.LEARNED_FILE}: {e}")

    def _read_learned_file(self):
        try:
            with open(self.LEARNED_FILE, "r") as f:
                return set(json.load(f).get("ips", []))
        except FileNotFoundError:
            return set()

    def _save_learned(self, learned_ips):
        """Save learned no-wake IPs to persistent file and queue a sync to the config UI."""
        try:
            with open(self.LEARNED_FILE, "w") as f:
                json.dump({"ips": sorted(learned_ips), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
        except Exception as e:
            log(f"No-wake learned: failed to save: {e}")
        self._request_config_sync()

    def _request_config_sync(self):
        # At most once a minute: each sync rewrites the add-on's whole config
        ha_worker.submit(self._sync_to_config, key="config:learned_nowake_ips", min_interval=60)

    def _sync_to_config(self):
        """Write learned IPs to the Supervisor config so they're visible in the UI."""
        if not SUPERVISOR_TOKEN:
            return
        try:
            with self.lock:
                learned = self._read_learned_file()
            update_addon_options({"learned_nowake_ips": ",".join(sorted(learned))})
        except Exception as e:
            log(f"No-wake learned: failed to sync to config: {e}")

    def _startup_sync(self):
        """On startup, sync learned IPs from file to the config UI (clearing it if none)."""
        self._request_config_sync()

    def learn(self, ip_str):
        """Learn a new no-wake IP. Returns True if newly added."""
        if not self.enabled:
            return False
        if self._is_excluded(ip_str) or ip_str in self.manual:
            return False
        try:
            if not ipaddress.ip_address(ip_str).is_global:
                return False  # local devices, CGNAT/Tailscale, etc. are never infrastructure
        except ValueError:
            return False

        with self.lock:
            if ip_str in self.ips:
                return False
            self.ips.add(ip_str)
            log(f"No-wake learned: added {ip_str}")

            # Load existing, merge, save
            try:
                existing = self._read_learned_file()
            except Exception:
                existing = set()
            existing.add(ip_str)
            self._save_learned(existing)
            return True

    def _discover_relay_ips(self):
        """Query plex.tv for relay server IPs and add them to the no-wake list."""
        try:
            req = urllib.request.Request(
                "https://plex.tv/api/v2/resources?includeRelay=1",
                headers={
                    "X-Plex-Token": self.admin_token,
                    "X-Plex-Client-Identifier": "plex-wol-listener",
                    "X-Plex-Product": "Plex WoL Listener",
                    "X-Plex-Version": VERSION,
                    "Accept": "application/json",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())

            relay_ips = set()
            for resource in data:
                connections = resource.get("connections", [])
                for conn in connections:
                    if conn.get("relay"):
                        addr = conn.get("address", "")
                        if addr:
                            relay_ips.add(addr)

            if relay_ips:
                with self.lock:
                    self.ips.update(relay_ips)
                log(f"No-wake auto-discover: found {len(relay_ips)} Plex relay IPs: {sorted(relay_ips)}")
            else:
                log("No-wake auto-discover: no relay IPs found")
        except Exception as e:
            log(f"No-wake auto-discover: failed to query plex.tv: {e}")

    def should_skip_wol(self, ip_str):
        if not self.enabled or self._is_excluded(ip_str):
            return False
        if ip_str in self.manual:
            return True
        with self.lock:
            return ip_str in self.ips

    def get_active_ips(self):
        with self.lock:
            return self.manual.entries() + sorted(self.ips - self.manual.ips)


# ---------------------------------------------------------------------------
# Flood detection
# ---------------------------------------------------------------------------
class FloodDetector:
    def __init__(self, threshold, window, excluded_ips=None):
        self.threshold = threshold
        self.window = window
        self.excluded_ips = excluded_ips or set()
        self.timestamps = []
        self.lock = threading.Lock()
        self.last_alert_time = 0.0
        self.alert_cooldown = 300

    def _is_excluded(self, ip_str):
        if ip_str in self.excluded_ips:
            return True
        try:
            return ipaddress.ip_address(ip_str).is_private
        except Exception:
            return False

    def record(self, addr):
        if self._is_excluded(addr[0]):
            return
        now = time.time()
        with self.lock:
            self.timestamps = [t for t in self.timestamps if now - t < self.window]
            self.timestamps.append(now)
            count = len(self.timestamps)
            if count > self.threshold and (now - self.last_alert_time) > self.alert_cooldown:
                self.last_alert_time = now
                msg = (f"{count} connections on port 32400 in the last "
                       f"{self.window}s. Latest from {addr[0]}.")
                log(f"ALERT: {msg}")
                notify("Plex WoL: Connection flood", msg, "flood")


# ---------------------------------------------------------------------------
# Burst detection for smart WoL
# ---------------------------------------------------------------------------
class BurstDetector:
    """Tracks per-IP connection bursts to distinguish cold opens from background polls.

    A cold app open generates many connections from one IP in quick succession
    (loading providers, hubs, libraries, thumbnails). A background poll is a
    single connection every few minutes. When enabled, WoL is only sent once a
    single IP reaches the burst count within the configured window.

    An IP whose only connection in a window was a lone probe is reported by
    pop_single_probes() after that window has passed, so it can be learned as
    infrastructure. An IP that connects again within the window (i.e. the start
    of a real cold open) is never reported.
    """

    def __init__(self, enabled, burst_count, burst_window, wake_timeout=120):
        self.enabled = enabled
        self.burst_count = max(2, burst_count)
        self.burst_window = burst_window
        self.timestamps = {}  # ip -> connection times within the window
        self.single_probes = {}  # ip -> time of a lone connection awaiting its window to pass
        self.lock = threading.Lock()
        # Once a burst triggers WoL, suppress further checks until
        # the server is confirmed up or the wake times out.
        self.wol_sent_recently = False
        self.wol_sent_time = 0.0
        self.wol_sent_cooldown = wake_timeout

    def _prune(self, now):
        for ip in list(self.timestamps):
            recent = [t for t in self.timestamps[ip] if now - t < self.burst_window]
            if recent:
                self.timestamps[ip] = recent
            else:
                del self.timestamps[ip]

    def record_and_check(self, ip_str):
        """Record a connection attempt while server is down.
        Returns True if burst threshold is met and WoL should be sent."""
        if not self.enabled:
            return True  # smart wol disabled, always send

        now = time.time()
        with self.lock:
            # If we already sent WoL recently, allow through (so clients
            # can wait for the server alongside the first burst)
            if self.wol_sent_recently and (now - self.wol_sent_time) < self.wol_sent_cooldown:
                self.single_probes.pop(ip_str, None)
                return True

            self._prune(now)
            stamps = self.timestamps.setdefault(ip_str, [])
            stamps.append(now)

            if len(stamps) == 1:
                self.single_probes.setdefault(ip_str, now)
            else:
                self.single_probes.pop(ip_str, None)

            if len(stamps) >= self.burst_count:
                self.wol_sent_recently = True
                self.wol_sent_time = now
                return True

            return False

    def pop_single_probes(self):
        """Return IPs whose lone connection's window passed with no follow-up."""
        if not self.enabled:
            return []
        now = time.time()
        with self.lock:
            expired = [ip for ip, t in self.single_probes.items() if now - t >= self.burst_window]
            for ip in expired:
                del self.single_probes[ip]
        return expired

    def reset(self):
        """Reset burst state when server comes up.

        Pending single probes are dropped too: once the server is up their
        follow-up connections are no longer seen here, so they can't be judged.
        """
        with self.lock:
            self.timestamps = {}
            self.single_probes = {}
            self.wol_sent_recently = False

    def get_count(self, ip_str):
        """Get current connection count in window for one IP (for logging)."""
        now = time.time()
        with self.lock:
            self._prune(now)
            return len(self.timestamps.get(ip_str, []))


# ---------------------------------------------------------------------------
# Sleep trigger
# ---------------------------------------------------------------------------
class SleepTrigger:
    def __init__(self, enabled, idle_minutes, max_awake_minutes, server_ip, ssh_user, ssh_port, toggle_manager=None):
        self.base_enabled = enabled and (idle_minutes > 0 or max_awake_minutes > 0) and bool(ssh_user)
        self.toggle_manager = toggle_manager
        self.idle_seconds = idle_minutes * 60 if idle_minutes > 0 else 0
        self.max_awake_seconds = max_awake_minutes * 60 if max_awake_minutes > 0 else 0
        self.server_ip = server_ip
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.last_activity = time.time()
        self.server_awake_since = None  # set when we first see the server up
        self.lock = threading.Lock()
        self.sleeping = False

        if self.base_enabled:
            self._ensure_ssh_key()
            t = threading.Thread(target=self._monitor_loop, daemon=True)
            t.start()
            parts = []
            if idle_minutes > 0:
                parts.append(f"{idle_minutes} min idle")
            if max_awake_minutes > 0:
                parts.append(f"{max_awake_minutes} min max awake")
            log(f"Sleep trigger: enabled ({', '.join(parts)} → sleep)")
        else:
            if not enabled:
                log("Sleep trigger: disabled")
            elif idle_minutes <= 0 and max_awake_minutes <= 0:
                log("Sleep trigger: disabled (sleep_idle_minutes and max_awake_minutes both 0)")
            elif not ssh_user:
                log("Sleep trigger: disabled (no server_ssh_user set)")

    @property
    def enabled(self):
        if self.toggle_manager:
            return self.base_enabled and self.toggle_manager.get_sleep()
        return self.base_enabled

    def _ensure_ssh_key(self):
        if not os.path.exists(SSH_KEY_PATH):
            log("Generating SSH key pair for sleep trigger…")
            try:
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", SSH_KEY_PATH, "-N", ""],
                    check=True, capture_output=True,
                )
                log("SSH key generated. Public key:")
                with open(SSH_KEY_PATH + ".pub", "r") as f:
                    pubkey = f.read().strip()
                log(f"  {pubkey}")
                log(f"  Add this to C:\\Users\\{self.ssh_user}\\.ssh\\authorized_keys on your Plex server")
            except Exception as e:
                log(f"ERROR: Failed to generate SSH key: {e}")
        else:
            with open(SSH_KEY_PATH + ".pub", "r") as f:
                pubkey = f.read().strip()
            log(f"SSH public key: {pubkey}")

    def touch(self, client_ip=None):
        # Skip sleep timer reset for the Plex server's own connections
        # and plex.tv cloud health checks (private IPs are local devices, fine to count)
        if client_ip and client_ip == self.server_ip:
            return
        with self.lock:
            self.last_activity = time.time()
            if self.sleeping:
                self.server_awake_since = time.time()
            elif self.server_awake_since is None:
                self.server_awake_since = time.time()
            self.sleeping = False

    def _server_is_up(self):
        try:
            probe = socket.create_connection((self.server_ip, 32400), timeout=2)
            probe.close()
            return True
        except Exception:
            return False

    def _send_sleep(self):
        log(f"Sleep trigger: sending sleep command to {self.server_ip}…")
        try:
            result = subprocess.run(
                [
                    "ssh", "-i", SSH_KEY_PATH,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10",
                    "-p", str(self.ssh_port),
                    f"{self.ssh_user}@{self.server_ip}",
                    "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
                ],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                log("Sleep trigger: sleep command sent successfully")
                self.sleeping = True
                notify("Plex WoL: Server sleeping",
                       f"Plex server {self.server_ip} put to sleep after idle timeout.",
                       "server_slept")
            else:
                stderr = result.stderr.decode().strip()
                log(f"Sleep trigger: SSH failed (rc={result.returncode}): {stderr}")
        except Exception as e:
            log(f"Sleep trigger: ERROR: {e}")

    def _monitor_loop(self):
        while True:
            time.sleep(60)
            if not self.enabled:
                continue
            with self.lock:
                idle_elapsed = time.time() - self.last_activity
                is_sleeping = self.sleeping
                awake_since = self.server_awake_since

            if is_sleeping:
                continue

            # Check idle timeout
            should_sleep_idle = self.idle_seconds > 0 and idle_elapsed >= self.idle_seconds

            # Check max awake timeout
            should_sleep_max = False
            if self.max_awake_seconds > 0 and awake_since:
                awake_elapsed = time.time() - awake_since
                should_sleep_max = awake_elapsed >= self.max_awake_seconds

            if should_sleep_idle or should_sleep_max:
                reason = "max awake time" if should_sleep_max else "idle timeout"
                if self._server_is_up():
                    log(f"Sleep trigger: {reason} reached — sleeping server")
                    self._send_sleep()
                else:
                    log("Sleep trigger: server already appears down, skipping")
                    with self.lock:
                        self.sleeping = True


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
def start_health_check(enabled, port, sensors, server_ip, server_port, conn_history, toggle_manager,
                       wake_history):
    if not enabled:
        log("Health check: disabled")
        return

    def check_server():
        try:
            probe = socket.create_connection((server_ip, server_port), timeout=2)
            probe.close()
            return True
        except Exception:
            return False

    def parse_request(data):
        """Parse raw HTTP request bytes into method, path (no query) and lower-cased headers."""
        try:
            lines = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
            method, target = lines[0].split(" ")[:2]
            headers = {}
            for line in lines[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            return method, target.split("?", 1)[0], headers
        except Exception:
            return "", "", {}

    def json_response(status_code, status_text, body_dict):
        body = json.dumps(body_dict, indent=2)
        return (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n{body}"
        ).encode()

    def handle_request(conn):
        try:
            raw = conn.recv(4096)
            if not raw:
                return
            method, path, headers = parse_request(raw)
            parts = [p for p in path.split("/") if p]

            # Browsers always send Origin on cross-site POSTs; HA's rest_command
            # doesn't. Refusing them stops any web page from flipping toggles.
            if method == "POST" and "origin" in headers:
                conn.sendall(json_response(403, "Forbidden", {
                    "error": "Browser requests are not allowed; use rest_command",
                }))

            # GET / — full status
            elif method == "GET" and parts in ([], ["status"]):
                uptime_str, uptime_secs = sensors.get_uptime()
                plex_reachable = check_server()
                history = conn_history.get_data()
                status = {
                    "status": "ok",
                    "uptime": uptime_str,
                    "uptime_seconds": uptime_secs,
                    "plex_server": server_ip,
                    "plex_server_reachable": plex_reachable,
                    "server_status": sensors.server_status,
                    "connection_count": sensors.connection_count,
                    "last_wake": sensors.last_wake_time or "never",
                    "last_wake_user": sensors.last_wake_user or "unknown",
                    "last_wake_outcome": (wake_history.get(1) or [{}])[0].get("outcome", "none"),
                    "unique_ips_today": history,
                    "toggles": toggle_manager.get_all(),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                conn.sendall(json_response(200, "OK", status))

            # GET /history — wake attempts, newest first
            elif method == "GET" and parts == ["history"]:
                conn.sendall(json_response(200, "OK", {"wakes": wake_history.get()}))

            # GET /toggles — toggle states only
            elif method == "GET" and parts == ["toggles"]:
                conn.sendall(json_response(200, "OK", toggle_manager.get_all()))

            # POST /toggle/<name> — flip a toggle
            elif method == "POST" and len(parts) == 2 and parts[0] == "toggle":
                name = parts[1]
                ok, new_val = toggle_manager.toggle(name)
                if ok:
                    conn.sendall(json_response(200, "OK", {
                        "toggled": name,
                        "value": new_val,
                        "all": toggle_manager.get_all(),
                    }))
                else:
                    conn.sendall(json_response(400, "Bad Request", {
                        "error": f"Unknown toggle: {name}",
                        "valid": list(toggle_manager.get_all().keys()),
                    }))

            # POST /toggle/<name>/on or /toggle/<name>/off — set explicitly
            elif (method == "POST" and len(parts) == 3 and parts[0] == "toggle"
                  and parts[2] in ("on", "off")):
                name = parts[1]
                ok, new_val = toggle_manager.set(name, parts[2] == "on")
                if ok:
                    conn.sendall(json_response(200, "OK", {
                        "set": name,
                        "value": new_val,
                        "all": toggle_manager.get_all(),
                    }))
                else:
                    conn.sendall(json_response(400, "Bad Request", {
                        "error": f"Unknown toggle: {name}",
                        "valid": list(toggle_manager.get_all().keys()),
                    }))

            else:
                conn.sendall(json_response(404, "Not Found", {"error": "Unknown endpoint"}))

        except Exception:
            pass
        finally:
            conn.close()

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", port))
        except OSError as e:
            log(f"Health check: failed to bind port {port}: {e}")
            return
        srv.listen(10)
        log(f"Health check: listening on port {port}")
        log(f"  Endpoints: GET /status, GET /history, GET /toggles, POST /toggle/<name>, "
            f"POST /toggle/<name>/on|off")
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle_request, args=(conn,), daemon=True).start()

    t = threading.Thread(target=serve, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Core networking
# ---------------------------------------------------------------------------
def send_wol(mac_str, broadcast="255.255.255.255", port=9):
    mac_str = mac_str.replace(":", "").replace("-", "").replace(".", "")
    if len(mac_str) != 12:
        raise ValueError(f"Invalid MAC address: {mac_str}")
    mac_bytes = bytes.fromhex(mac_str)
    packet = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(packet, (broadcast, port))
    sock.close()


def load_options():
    defaults = {
        "plex_server_ip": "",
        "target_mac": "",
        "broadcast": "255.255.255.255",
        "enable_wol": True,
        "listen_port": 32400,
        "plex_server_port": 32400,
        "rate_limit_seconds": 30,
        "wake_timeout_seconds": 120,
        "flood_threshold": 20,
        "flood_window_seconds": 60,
        "enable_geoip": True,
        "allowed_countries": "US",
        "enable_ip_allowlist": False,
        "ip_allowlist": "",
        "enable_ip_blocklist": False,
        "ip_blocklist": "",
        "enable_nowake_list": False,
        "nowake_list": "",
        "auto_discover_plex_relays": False,
        "relay_rediscover_hours": 6,
        "allow_ip_plex_relay": "",
        "enable_sleep_trigger": False,
        "sleep_idle_minutes": 0,
        "max_awake_minutes": 0,
        "server_ssh_user": "",
        "server_ssh_port": 22,
        "enable_health_check": True,
        "health_check_port": 32401,
        "enable_ha_sensors": True,
        "notify_target": "",
        "notify_wake_timeout": True,
        "notify_flood": True,
        "notify_server_slept": True,
        "notify_server_woken": True,
        "notify_geoip_block": False,
        "enable_file_logging": True,
        "log_retention_days": 14,
        "log_dedup_cooldown_seconds": 300,
        "enable_quiet_mode": False,
        "user_friendly_names": "",
        "enable_smart_wol": False,
        "smart_wol_burst_count": 3,
        "smart_wol_burst_window": 15,
        "enable_user_tracking": False,
        "plex_admin_token": "",
    }
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            file_opts = json.load(f) or {}
        defaults.update({k: v for k, v in file_opts.items() if v is not None and v != ""})
    except Exception as e:
        print(f"WARNING: Could not read {OPTIONS_PATH}: {e}. Using defaults.", flush=True)
    return defaults


def is_local_address(host):
    """True if `host` is an address of this machine (i.e. we can bind to it)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
        return True
    except OSError:
        return False


def sanitize_network_opts(opts):
    """Blank out placeholder or dangerous server/WoL settings in place.

    Returns a list of problems found, for logging.
    """
    problems = []
    server_ip = str(opts.get("plex_server_ip", "")).strip()
    server_port = int(opts.get("plex_server_port", 32400))
    listen_port = int(opts.get("listen_port", 32400))
    if server_ip and server_port == listen_port and is_local_address(server_ip):
        # 0.0.0.0, 127.x or this host's own IP: every proxied connection would
        # connect back into the proxy and spawn another, without end
        problems.append(f"plex_server_ip {server_ip} is this Home Assistant host, so the proxy "
                        f"would connect to itself — proxying disabled until it's set to the Plex server's IP")
        server_ip = ""
    elif not server_ip:
        problems.append("plex_server_ip is not set — the add-on won't do anything useful")
    opts["plex_server_ip"] = server_ip

    mac = str(opts.get("target_mac", "")).strip()
    mac_hex = mac.replace(":", "").replace("-", "").replace(".", "")
    if mac and (len(mac_hex) != 12 or any(c not in "0123456789abcdefABCDEF" for c in mac_hex)):
        problems.append(f"target_mac '{mac}' is not a valid MAC address — WoL disabled")
        mac = ""
    elif mac and int(mac_hex, 16) == 0:
        problems.append("target_mac is the 00:00:00:00:00:00 placeholder — WoL disabled")
        mac = ""
    opts["target_mac"] = mac

    broadcast = str(opts.get("broadcast", "")).strip()
    if broadcast in ("", "0.0.0.0"):
        problems.append("broadcast is not set — using 255.255.255.255 (set your subnet "
                        "broadcast, e.g. 192.168.1.255, if WoL doesn't reach the server)")
        broadcast = "255.255.255.255"
    opts["broadcast"] = broadcast
    return problems


def parse_friendly_names(names_str):
    """Parse 'plexuser:Friendly Name,other:Other Name' into a dict."""
    mapping = {}
    if not names_str:
        return mapping
    for entry in names_str.split(","):
        entry = entry.strip()
        if ":" in entry:
            plex_name, friendly = entry.split(":", 1)
            mapping[plex_name.strip().lower()] = friendly.strip()
    return mapping


def get_friendly_name(plex_user, name_map):
    """Return the friendly name for a Plex user, or the original username."""
    if not plex_user:
        return None
    return name_map.get(plex_user.lower(), plex_user)


def proxy_data(client_sock, upstream_sock):
    # selectors (epoll) rather than select(), which fails on file descriptors >= 1024
    peers = {client_sock: upstream_sock, upstream_sock: client_sock}
    sel = selectors.DefaultSelector()
    try:
        for s in peers:
            sel.register(s, selectors.EVENT_READ)
        while True:
            for key, _ in sel.select(timeout=30):
                s = key.fileobj
                try:
                    data = s.recv(PROXY_BUF)
                except Exception:
                    data = b""
                if not data:
                    return
                try:
                    peers[s].sendall(data)
                except Exception:
                    return
    finally:
        sel.close()
        for s in peers:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Waking: readiness check, shared waiter, history
# ---------------------------------------------------------------------------
# Wake outcomes
WAKE_READY = "ready"  # Plex answered /identity
WAKE_UNVERIFIED = "unverified"  # port open but /identity never answered; proxied anyway
WAKE_PLEX_NOT_RUNNING = "plex_not_running"  # the PC answered but nothing listens on the Plex port
WAKE_TIMEOUT = "timeout"  # no answer at all
WAKE_PROXIED = (WAKE_READY, WAKE_UNVERIFIED)

_insecure_ssl = ssl.create_default_context()
_insecure_ssl.check_hostname = False  # the plex.direct cert won't match a LAN IP
_insecure_ssl.verify_mode = ssl.CERT_NONE
# LAN requests must never go through an HTTP(S)_PROXY from the environment
_lan_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_insecure_ssl))


def probe_port(ip, port, timeout=2):
    """'open', 'refused' (the host is awake but nothing is listening) or 'down' (no answer)."""
    try:
        socket.create_connection((ip, port), timeout=timeout).close()
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "down"


def plex_identity_ok(ip, port, timeout=3):
    """True once Plex itself answers, not just its port: GET /identity returns 200."""
    for scheme in ("https", "http"):
        try:
            with _lan_opener.open(f"{scheme}://{ip}:{port}/identity", timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


def wait_until_ready(ip, port, timeout):
    """Poll until Plex answers or `timeout` passes. Returns a WAKE_* outcome."""
    deadline = time.time() + timeout
    while True:
        state = probe_port(ip, port)
        if state == "open" and plex_identity_ok(ip, port):
            return WAKE_READY
        if time.time() >= deadline:
            return {"open": WAKE_UNVERIFIED, "refused": WAKE_PLEX_NOT_RUNNING}.get(state, WAKE_TIMEOUT)
        time.sleep(CONNECT_POLL_INTERVAL)


class WakeHistory:
    """Persisted record of wake attempts, newest last, exposed via /history and a sensor."""

    HISTORY_FILE = "/data/wake_history.json"
    MAX_ENTRIES = 100
    SENSOR_ENTRIES = 5

    def __init__(self, publish):
        self.publish_enabled = publish
        self.lock = threading.Lock()
        self.entries = []
        try:
            with open(self.HISTORY_FILE, "r") as f:
                self.entries = json.load(f).get("wakes", [])[-self.MAX_ENTRIES:]
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Wake history: failed to load {self.HISTORY_FILE}: {e}")
        self._publish()

    def add(self, **fields):
        """Record a wake attempt. Returns its id, for update()."""
        with self.lock:
            entry_id = (self.entries[-1]["id"] + 1) if self.entries else 1
            self.entries.append(dict(fields, id=entry_id, time=time.strftime("%Y-%m-%d %H:%M:%S")))
            del self.entries[:-self.MAX_ENTRIES]
            self._save()
        self._publish()
        return entry_id

    def update(self, entry_id, **fields):
        with self.lock:
            for entry in self.entries:
                if entry["id"] == entry_id:
                    entry.update(fields)
                    self._save()
                    break
        self._publish()

    def get(self, limit=None):
        """Entries newest first."""
        with self.lock:
            entries = [dict(e) for e in reversed(self.entries)]
        return entries[:limit] if limit else entries

    def _save(self):
        try:
            with open(self.HISTORY_FILE, "w") as f:
                json.dump({"wakes": self.entries}, f, indent=2)
        except Exception as e:
            log(f"Wake history: failed to save: {e}")

    def _publish(self):
        if not self.publish_enabled:
            return
        recent = self.get(self.SENSOR_ENTRIES)
        today = time.strftime("%Y-%m-%d")
        with self.lock:
            wakes_today = sum(1 for e in self.entries if e["time"].startswith(today))
        post_state("sensor.plex_wol_wake_history", recent[0]["outcome"] if recent else "none", {
            "friendly_name": "Plex WoL Wake History",
            "icon": "mdi:history",
            "wakes_today": wakes_today,
            "recent": recent,
        })


class ServerWaiter:
    """Waits for the Plex server after a wake, shared by every client that's waiting.

    The first client to wait starts a single probe loop; clients arriving while
    it runs wait for that result instead of probing on their own. Only that
    first wait reports the outcome: one status update, one notification, one
    history entry per wake.
    """

    def __init__(self, server_ip, server_port, timeout, sensors, history):
        self.server_ip = server_ip
        self.server_port = server_port
        self.timeout = timeout
        self.sensors = sensors
        self.history = history
        self.lock = threading.Lock()
        self.cycle = None  # the wake currently being waited on

    def wait(self, trigger_ip, wol_sent):
        """Block until the server is ready or the wake times out.

        Returns (outcome, history entry id); the id is None for clients that
        joined a wake another client was already waiting on.
        """
        with self.lock:
            cycle = self.cycle
            first = cycle is None
            if first:
                cycle = self.cycle = {"done": threading.Event(), "outcome": WAKE_TIMEOUT}
        if not first:
            cycle["done"].wait(self.timeout + 30)
            return cycle["outcome"], None

        started = time.time()
        try:
            cycle["outcome"] = wait_until_ready(self.server_ip, self.server_port, self.timeout)
        finally:
            with self.lock:
                self.cycle = None
            cycle["done"].set()
        outcome = cycle["outcome"]
        return outcome, self._report(outcome, trigger_ip, wol_sent, time.time() - started)

    def _report(self, outcome, trigger_ip, wol_sent, elapsed):
        server = f"{self.server_ip}:{self.server_port}"
        if outcome == WAKE_READY:
            log(f"Wake: Plex ready after {elapsed:.0f}s")
        elif outcome == WAKE_UNVERIFIED:
            log(f"Wake: WARNING — port {self.server_port} is open but Plex didn't answer "
                f"/identity within {self.timeout}s; proxying anyway")
        elif outcome == WAKE_PLEX_NOT_RUNNING:
            msg = (f"The Plex server PC at {self.server_ip} is awake, but nothing answered on port "
                   f"{self.server_port} within {self.timeout}s. Plex Media Server may not be running. "
                   f"Connection from {trigger_ip} dropped.")
            log(f"Wake: PLEX NOT RUNNING: {msg}")
            notify("Plex WoL: Plex not running", msg, "wake_timeout")
        else:
            msg = (f"Plex server {server} did not respond within {self.timeout}s"
                   f"{' after WoL' if wol_sent else ''}. Connection from {trigger_ip} dropped.")
            log(f"Wake: TIMEOUT: {msg}")
            notify("Plex WoL: Wake failed", msg, "wake_timeout")

        self.sensors.set_server_status("up" if outcome in WAKE_PROXIED else outcome)
        return self.history.add(
            trigger_ip=trigger_ip,
            wol_sent=wol_sent,
            outcome=outcome,
            seconds_to_ready=round(elapsed, 1) if outcome in WAKE_PROXIED else None,
            user=None,
        )



# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
class ConnectionTracker:
    """Tracks active proxy connections for graceful shutdown."""

    def __init__(self):
        self.active = set()
        self.lock = threading.Lock()

    def add(self, sock):
        with self.lock:
            self.active.add(sock)

    def remove(self, sock):
        with self.lock:
            self.active.discard(sock)

    def close_all(self):
        with self.lock:
            sockets = list(self.active)
            self.active.clear()
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass
        return len(sockets)


# Global tracker and server socket for signal handler
connection_tracker = ConnectionTracker()
server_socket = None


def shutdown_handler(signum, frame):
    log(f"Received signal {signum} — shutting down gracefully…")
    count = connection_tracker.close_all()
    log(f"Closed {count} active connections.")
    if server_socket:
        try:
            server_socket.close()
        except Exception:
            pass
    log("Shutdown complete.")
    os._exit(0)


# ---------------------------------------------------------------------------
# Client handler
# ---------------------------------------------------------------------------
def handle_client(client_sock, addr, opts, wol_state, flood, geoip, allowlist, blocklist,
                  sensors, sleeper, toggle_manager, session_tracker, conn_history, burst_detector,
                  nowake_list, name_map, server_waiter):
    # Track connection
    connection_tracker.add(client_sock)

    try:
        _handle_client_inner(client_sock, addr, opts, wol_state, flood, geoip,
                             allowlist, blocklist, sensors, sleeper, toggle_manager, session_tracker,
                             conn_history, burst_detector, nowake_list, name_map, server_waiter)
    except Exception as e:
        log(f"[{addr}] ERROR handling connection: {e}")
    finally:
        connection_tracker.remove(client_sock)
        client_sock.close()


def _handle_client_inner(client_sock, addr, opts, wol_state, flood, geoip,
                         allowlist, blocklist, sensors, sleeper, toggle_manager, session_tracker,
                         conn_history, burst_detector, nowake_list, name_map, server_waiter):
    # Flood detection
    flood.record(addr)

    # Sensor: count connection
    sensors.increment_connections()

    client_ip = addr[0]

    # Connection history
    conn_history.record(client_ip)

    quiet = toggle_manager.get_quiet()

    # IP blocklist check (before allowlist)
    if blocklist.is_blocked(client_ip):
        client_sock.close()
        return

    # IP allowlist check
    if not allowlist.is_allowed(client_ip):
        client_sock.close()
        return

    # GeoIP check
    if not geoip.is_allowed(client_ip):
        client_sock.close()
        return

    target_mac = opts["target_mac"]
    broadcast = opts["broadcast"]
    server_ip = opts["plex_server_ip"]
    server_port = int(opts["plex_server_port"])
    rate_limit = int(opts["rate_limit_seconds"])
    wake_timeout = int(opts["wake_timeout_seconds"])

    if not server_ip:
        if wol_disabled_dedup.should_log("no_server_ip"):
            log(f"[{addr}] No usable plex_server_ip configured — dropping connections "
                f"(suppressing repeats for {wol_disabled_dedup.cooldown}s)")
        client_sock.close()
        return

    # Touch sleep trigger on every connection
    if sleeper:
        sleeper.touch(client_ip)

    user_tag = ""

    # Connect straight to the real server; failing that, it's down
    try:
        upstream = socket.create_connection((server_ip, server_port), timeout=2)
    except OSError:
        upstream = None

    if upstream:
        if not quiet:
            log(f"[{addr}]{user_tag} Server already up — proxying to {server_ip}:{server_port}")
        sensors.set_server_status("up")
        burst_detector.reset()
    else:
        # Auto-learn: IPs that sent a lone probe and nothing else within the
        # burst window while the server was down are infrastructure
        for probe_ip in burst_detector.pop_single_probes():
            nowake_list.learn(probe_ip)

        # No-wake list: proxy when up, silently drop when down
        if nowake_list.should_skip_wol(client_ip):
            client_sock.close()
            return

        # Smart WoL: check for connection burst — runs regardless of WoL toggle
        # so that learning always works even in test mode
        burst_ok = burst_detector.record_and_check(client_ip)

        if not burst_ok:
            count = burst_detector.get_count(client_ip)
            log(f"[{addr}]{user_tag} Server down — background poll ignored by smart WoL "
                f"({count}/{burst_detector.burst_count} connections in window)")
            client_sock.close()
            return

        # Check if WoL is enabled via config and dashboard toggle
        wol_config_enabled = bool(opts.get("enable_wol", True))
        wol_dashboard_enabled = toggle_manager.get_wol()
        wol_enabled = wol_config_enabled and wol_dashboard_enabled

        now = time.time()
        wol_sent = False
        with wol_state["lock"]:
            elapsed = now - wol_state["last_wol"]
            if not wol_enabled:
                reason = "config" if not wol_config_enabled else "dashboard toggle"
                if wol_disabled_dedup.should_log(client_ip):
                    log(f"[{addr}]{user_tag} Server down — WoL disabled via {reason}, dropping connection (suppressing repeats for {wol_disabled_dedup.cooldown}s)")
                sensors.set_server_status("down")
                client_sock.close()
                return
            elif target_mac and elapsed >= rate_limit:
                log(f"[{addr}]{user_tag} Server down — burst detected, sending WoL to {target_mac} via {broadcast}")
                try:
                    send_wol(target_mac, broadcast)
                    wol_state["last_wol"] = now
                    wol_sent = True
                    sensors.set_last_wake(None)
                except Exception as e:
                    log(f"[{addr}]{user_tag} ERROR sending WoL: {e}")
            elif not target_mac:
                log(f"[{addr}]{user_tag} Server down and no target_mac set — cannot send WoL")
            else:
                log(f"[{addr}]{user_tag} Server down — WoL rate-limited ({rate_limit - elapsed:.0f}s left)")

        sensors.set_server_status("waking")
        log(f"[{addr}]{user_tag} Waiting up to {wake_timeout}s for server to come up…")
        outcome, history_id = server_waiter.wait(client_ip, wol_sent)
        if outcome not in WAKE_PROXIED:
            client_sock.close()
            return
        log(f"[{addr}]{user_tag} Server is up — proxying.")
        burst_detector.reset()

        # Track who woke the server via session query (once per wake)
        if history_id and session_tracker.enabled:
            def _track_wake_user():
                wake_user = session_tracker.get_wake_user(delay=10)
                if wake_user:
                    display = get_friendly_name(wake_user, name_map)
                    log(f"[{addr}] Wake triggered by user: {display}")
                    sensors.set_last_wake(display)
                    server_waiter.history.update(history_id, user=display)
                    notify("Plex WoL: Server woken", f"Server woken by {display} from {client_ip}",
                           "server_woken")
            threading.Thread(target=_track_wake_user, daemon=True).start()

        try:
            upstream = socket.create_connection((server_ip, server_port), timeout=10)
        except Exception as e:
            log(f"[{addr}] Failed to connect to {server_ip}:{server_port}: {e}")
            client_sock.close()
            return

    upstream.settimeout(None)  # the connect timeout shouldn't apply to proxied reads/writes

    connection_tracker.add(upstream)

    proxy_data(client_sock, upstream)
    connection_tracker.remove(upstream)
    if not quiet:
        log(f"[{addr}]{user_tag} Connection closed.")

    # Touch sleep trigger when connection ends too
    if sleeper:
        sleeper.touch(client_ip)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global server_socket

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    opts = load_options()

    # Setup file logging first
    setup_file_logging(
        bool(opts.get("enable_file_logging", True)),
        int(opts.get("log_retention_days", 14)),
    )

    listen_port = int(opts.get("listen_port", 32400))
    flood_threshold = max(5, int(opts.get("flood_threshold", 10)))
    flood_window = int(opts.get("flood_window_seconds", 60))

    dedup_cooldown = int(opts.get("log_dedup_cooldown_seconds", 300))
    wol_disabled_dedup.set_cooldown(dedup_cooldown)

    global NOTIFY_TARGET
    NOTIFY_TARGET = str(opts.get("notify_target", "")).strip()

    global NOTIFY_ENABLED
    NOTIFY_ENABLED = {
        "wake_timeout": bool(opts.get("notify_wake_timeout", True)),
        "flood": bool(opts.get("notify_flood", True)),
        "server_slept": bool(opts.get("notify_server_slept", True)),
        "server_woken": bool(opts.get("notify_server_woken", True)),
        "geoip_block": bool(opts.get("notify_geoip_block", False)),
    }

    ha_worker.start()

    # --- Config dependency enforcement ---
    corrections = {}

    # Auto-discover requires no-wake list
    if opts.get("auto_discover_plex_relays") and not opts.get("enable_nowake_list"):
        corrections["enable_nowake_list"] = True
        log("Config: auto_discover_plex_relays requires enable_nowake_list — auto-enabled")

    # Auto-discover requires admin token
    if opts.get("auto_discover_plex_relays") and not opts.get("plex_admin_token"):
        corrections["auto_discover_plex_relays"] = False
        log("Config: auto_discover_plex_relays requires plex_admin_token — auto-disabled")

    # User tracking requires admin token
    if opts.get("enable_user_tracking") and not opts.get("plex_admin_token"):
        corrections["enable_user_tracking"] = False
        log("Config: enable_user_tracking requires plex_admin_token — auto-disabled")

    # Sleep trigger requires SSH user
    if opts.get("enable_sleep_trigger") and not opts.get("server_ssh_user"):
        corrections["enable_sleep_trigger"] = False
        log("Config: enable_sleep_trigger requires server_ssh_user — auto-disabled")

    opts.update(corrections)

    # Persist corrected config to Supervisor so UI reflects changes
    if corrections and SUPERVISOR_TOKEN:
        try:
            update_addon_options(corrections)
            log("Config: corrected settings saved to Supervisor")
        except Exception as e:
            log(f"Config: WARNING — failed to save corrected settings: {e}")

    for problem in sanitize_network_opts(opts):
        log(f"*** Config: {problem} ***")

    log(f"=== Plex WoL Proxy v{VERSION} ===")
    log(f"  Listen:           0.0.0.0:{listen_port}")
    log(f"  Plex server:      {opts.get('plex_server_ip') or '(NOT SET)'}:{opts.get('plex_server_port', 32400)}")
    log(f"  Target MAC:       {opts.get('target_mac') or '(NOT SET)'}")
    log(f"  Broadcast:        {opts.get('broadcast')}")
    log(f"  WoL:              {'ON' if opts.get('enable_wol', True) else 'OFF'}")
    log(f"  Rate limit:       {opts.get('rate_limit_seconds')}s")
    log(f"  Wake timeout:     {opts.get('wake_timeout_seconds')}s")
    log(f"  Flood alert:      >{flood_threshold} in {flood_window}s")
    log(f"  User tracking:    {'ON' if opts.get('enable_user_tracking') and opts.get('plex_admin_token') else 'OFF'}")
    log(f"  Smart WoL:        {'ON — ' + str(opts.get('smart_wol_burst_count', 3)) + ' connections in ' + str(opts.get('smart_wol_burst_window', 15)) + 's' if opts.get('enable_smart_wol') else 'OFF'}")
    log(f"  GeoIP:            {'ON — ' + opts.get('allowed_countries', 'US') if opts.get('enable_geoip') else 'OFF'}")
    log(f"  IP allowlist:     {'ON — ' + opts.get('ip_allowlist', '') if opts.get('enable_ip_allowlist') else 'OFF'}")
    log(f"  IP blocklist:     {'ON — ' + opts.get('ip_blocklist', '') if opts.get('enable_ip_blocklist') else 'OFF'}")
    log(f"  No-wake list:     {'ON — ' + opts.get('nowake_list', '') if opts.get('enable_nowake_list') else 'OFF'}")
    log(f"  Relay auto-disc:  {'ON' if opts.get('auto_discover_plex_relays') else 'OFF'}")
    log(f"  Log dedup:        {dedup_cooldown}s cooldown")
    log(f"  Sleep trigger:    {'ON — idle:' + str(opts.get('sleep_idle_minutes', 0)) + 'min, max awake:' + str(opts.get('max_awake_minutes', 0)) + 'min' if opts.get('enable_sleep_trigger') else 'OFF'}")
    log(f"  Health check:     {'ON — port ' + str(opts.get('health_check_port', 32401)) if opts.get('enable_health_check') else 'OFF'}")
    log(f"  HA sensors:       {'ON' if opts.get('enable_ha_sensors') else 'OFF'}")
    log(f"  Dashboard toggles:ON (via health check port {opts.get('health_check_port', 32401)})")
    log(f"  File logging:     {'ON — ' + str(opts.get('log_retention_days', 14)) + ' day retention' if opts.get('enable_file_logging') else 'OFF'}")
    log(f"  Quiet mode:       {'ON' if opts.get('enable_quiet_mode') else 'OFF'}")
    log(f"  Notify target:    {NOTIFY_TARGET or '(persistent only)'}")
    log(f"  HA API token:     {'present' if SUPERVISOR_TOKEN else 'MISSING'}")

    # Initialize all modules
    wol_state = {"last_wol": 0.0, "lock": threading.Lock()}
    plex_server_ip = str(opts.get("plex_server_ip", ""))
    flood_excluded = {plex_server_ip} if plex_server_ip else set()
    flood = FloodDetector(flood_threshold, flood_window, excluded_ips=flood_excluded)

    # Toggle manager (file-persisted, controlled via health check HTTP endpoint)
    toggle_manager = ToggleManager(
        initial_wol=bool(opts.get("enable_wol", True)),
        initial_geoip=bool(opts.get("enable_geoip", True)),
        initial_quiet=bool(opts.get("enable_quiet_mode", False)),
        initial_sleep=bool(opts.get("enable_sleep_trigger", False)),
    )

    geoip = GeoIPChecker(
        bool(opts.get("enable_geoip", True)),
        str(opts.get("allowed_countries", "US")),
        toggle_manager=toggle_manager,
    )

    allowlist = IPAllowlist(
        bool(opts.get("enable_ip_allowlist", False)),
        str(opts.get("ip_allowlist", "")),
    )

    blocklist = IPBlocklist(
        bool(opts.get("enable_ip_blocklist", False)),
        str(opts.get("ip_blocklist", "")),
    )

    nowake_list = NoWakeList(
        bool(opts.get("enable_nowake_list", False)),
        str(opts.get("nowake_list", "")),
        auto_discover=bool(opts.get("auto_discover_plex_relays", False)),
        admin_token=str(opts.get("plex_admin_token", "")),
        exclude_str=str(opts.get("allow_ip_plex_relay", "")),
        rediscover_hours=int(opts.get("relay_rediscover_hours", 6)),
    )
    if nowake_list.enabled:
        active = nowake_list.get_active_ips()
        log(f"  No-wake active:   {active if active else '(none)'}")

    sensors = HASensors(bool(opts.get("enable_ha_sensors", True)))
    sensors.set_server_status("unknown")
    sensors.publish_all()

    conn_history = ConnectionHistory(bool(opts.get("enable_ha_sensors", True)))

    server_waiter = ServerWaiter(
        server_ip=str(opts.get("plex_server_ip", "")),
        server_port=int(opts.get("plex_server_port", 32400)),
        timeout=int(opts.get("wake_timeout_seconds", 120)),
        sensors=sensors,
        history=WakeHistory(bool(opts.get("enable_ha_sensors", True))),
    )

    session_tracker = PlexSessionTracker(
        enabled=bool(opts.get("enable_user_tracking", False)),
        server_ip=str(opts.get("plex_server_ip", "")),
        server_port=int(opts.get("plex_server_port", 32400)),
        admin_token=str(opts.get("plex_admin_token", "")),
    )

    burst_detector = BurstDetector(
        enabled=bool(opts.get("enable_smart_wol", False)),
        burst_count=int(opts.get("smart_wol_burst_count", 3)),
        burst_window=int(opts.get("smart_wol_burst_window", 15)),
        wake_timeout=int(opts.get("wake_timeout_seconds", 120)),
    )

    name_map = parse_friendly_names(str(opts.get("user_friendly_names", "")))
    if name_map:
        log(f"  Friendly names:   {name_map}")

    sleeper = SleepTrigger(
        enabled=bool(opts.get("enable_sleep_trigger", False)),
        idle_minutes=int(opts.get("sleep_idle_minutes", 0)),
        max_awake_minutes=int(opts.get("max_awake_minutes", 0)),
        server_ip=str(opts.get("plex_server_ip", "")),
        ssh_user=str(opts.get("server_ssh_user", "")),
        ssh_port=int(opts.get("server_ssh_port", 22)),
        toggle_manager=toggle_manager,
    )

    start_health_check(
        bool(opts.get("enable_health_check", True)),
        int(opts.get("health_check_port", 32401)),
        sensors,
        str(opts.get("plex_server_ip", "")),
        int(opts.get("plex_server_port", 32400)),
        conn_history,
        toggle_manager,
        server_waiter.history,
    )

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(64)
    server_socket = srv
    log("Listening…")

    while True:
        try:
            client_sock, addr = srv.accept()
        except OSError:
            break  # Socket closed during shutdown
        t = threading.Thread(
            target=handle_client,
            args=(client_sock, addr, opts, wol_state, flood, geoip, allowlist, blocklist,
                  sensors, sleeper, toggle_manager, session_tracker, conn_history, burst_detector,
                  nowake_list, name_map, server_waiter),
            daemon=True,
        )
        t.start()


if __name__ == "__main__":
    main()
