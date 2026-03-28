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

import ipaddress
import json
import logging
import logging.handlers
import os
import signal
import socket
import select
import subprocess
import threading
import time
import urllib.request
import urllib.parse

OPTIONS_PATH = "/data/options.json"
PROXY_BUF = 65536
CONNECT_POLL_INTERVAL = 2
SSH_KEY_PATH = "/data/plex_wol_key"
LOG_FILE_PATH = "/data/plex_wol.log"

HA_API = "http://supervisor/core/api"
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


def ha_api_get(endpoint):
    """GET from HA API. Returns parsed JSON or None."""
    if not SUPERVISOR_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        req = urllib.request.Request(f"{HA_API}{endpoint}", headers=headers)
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def ha_notify(title, message):
    """Send notification via HA persistent notification + mobile app."""
    ha_api_post("/services/persistent_notification/create", {
        "title": title,
        "message": message,
        "notification_id": "plex_wol_" + str(int(time.time())),
    })
    ha_api_post("/services/notify/mobile_app_pixel_10_pro", {
        "title": title,
        "message": message,
    })


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

    def _post_state(self, entity_id, state, attributes=None):
        if not self.enabled:
            return
        ha_api_post(f"/states/{entity_id}", {
            "state": state,
            "attributes": attributes or {},
        })

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
        })

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
            self.unique_ips.add(ip_str)
            self._publish()

    def _publish(self):
        ip_list = sorted(self.unique_ips)
        ha_api_post(f"/states/sensor.plex_wol_unique_ips_today", {
            "state": str(len(ip_list)),
            "attributes": {
                "friendly_name": "Plex WoL Unique IPs Today",
                "icon": "mdi:ip-network",
                "date": self.today,
                "ips": ip_list,
            },
        })

    def get_data(self):
        with self.lock:
            return {"date": self.today, "count": len(self.unique_ips), "ips": sorted(self.unique_ips)}


# ---------------------------------------------------------------------------
# HA Dashboard toggles
# ---------------------------------------------------------------------------
class DashboardToggles:
    """Creates and polls input_boolean entities for live dashboard control."""

    TOGGLES = {
        "input_boolean.plex_wol_enabled": ("Plex WoL: WoL Enabled", "mdi:power"),
        "input_boolean.plex_wol_geoip": ("Plex WoL: GeoIP Blocking", "mdi:earth"),
        "input_boolean.plex_wol_quiet_mode": ("Plex WoL: Quiet Mode", "mdi:volume-off"),
        "input_boolean.plex_wol_sleep_trigger": ("Plex WoL: Sleep Trigger", "mdi:sleep"),
    }

    def __init__(self, enabled, initial_wol, initial_geoip, initial_quiet, initial_sleep):
        self.enabled = enabled
        self.wol = initial_wol
        self.geoip = initial_geoip
        self.quiet = initial_quiet
        self.sleep = initial_sleep
        self.lock = threading.Lock()

        if not enabled:
            return

        # Create input_boolean entities via HA API
        for entity_id, (name, icon) in self.TOGGLES.items():
            initial = {
                "input_boolean.plex_wol_enabled": initial_wol,
                "input_boolean.plex_wol_geoip": initial_geoip,
                "input_boolean.plex_wol_quiet_mode": initial_quiet,
                "input_boolean.plex_wol_sleep_trigger": initial_sleep,
            }.get(entity_id, False)

            state = "on" if initial else "off"
            ha_api_post(f"/states/{entity_id}", {
                "state": state,
                "attributes": {"friendly_name": name, "icon": icon},
            })

        # Start polling thread
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        log("Dashboard toggles: enabled")

    def _poll_loop(self):
        while True:
            time.sleep(5)
            try:
                self._sync("input_boolean.plex_wol_enabled", "wol")
                self._sync("input_boolean.plex_wol_geoip", "geoip")
                self._sync("input_boolean.plex_wol_quiet_mode", "quiet")
                self._sync("input_boolean.plex_wol_sleep_trigger", "sleep")
            except Exception:
                pass

    def _sync(self, entity_id, attr_name):
        result = ha_api_get(f"/states/{entity_id}")
        if result and "state" in result:
            new_val = result["state"] == "on"
            with self.lock:
                old_val = getattr(self, attr_name)
                if new_val != old_val:
                    setattr(self, attr_name, new_val)
                    log(f"Dashboard toggle: {entity_id} changed to {'ON' if new_val else 'OFF'}")

    def get_wol(self):
        with self.lock:
            return self.wol

    def get_geoip(self):
        with self.lock:
            return self.geoip

    def get_quiet(self):
        with self.lock:
            return self.quiet

    def get_sleep(self):
        with self.lock:
            return self.sleep


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
class GeoIPChecker:
    def __init__(self, enabled, allowed_countries_str, toggles=None):
        self.base_enabled = enabled
        self.allowed = set()
        self.toggles = toggles
        if allowed_countries_str:
            self.allowed = {c.strip().upper() for c in allowed_countries_str.split(",")}
        self.cache = {}
        self.lock = threading.Lock()

    @property
    def enabled(self):
        if self.toggles:
            return self.toggles.get_geoip()
        return self.base_enabled

    def is_private(self, ip_str):
        try:
            return ipaddress.ip_address(ip_str).is_private
        except Exception:
            return False

    def is_allowed(self, ip_str):
        if not self.enabled:
            return True
        if self.is_private(ip_str):
            return True

        with self.lock:
            if ip_str in self.cache:
                return self.cache[ip_str]

        allowed = True
        try:
            url = f"http://ip-api.com/json/{ip_str}?fields=status,countryCode"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=5)
            result = json.loads(resp.read().decode())
            if result.get("status") == "success":
                country = result.get("countryCode", "").upper()
                allowed = country in self.allowed
                if not allowed:
                    log(f"GeoIP BLOCKED: {ip_str} is from {country} (allowed: {self.allowed})")
        except Exception as e:
            log(f"WARNING: GeoIP lookup failed for {ip_str}: {e} — allowing by default")
            allowed = True

        with self.lock:
            self.cache[ip_str] = allowed
        return allowed


# ---------------------------------------------------------------------------
# IP Allowlist
# ---------------------------------------------------------------------------
class IPAllowlist:
    def __init__(self, enabled, allowlist_str):
        self.enabled = enabled
        self.allowed = set()
        if allowlist_str:
            self.allowed = {ip.strip() for ip in allowlist_str.split(",") if ip.strip()}

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
        self.blocked = set()
        if blocklist_str:
            self.blocked = {ip.strip() for ip in blocklist_str.split(",") if ip.strip()}

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

    def __init__(self, enabled, nowake_str):
        self.enabled = enabled
        self.ips = set()
        if nowake_str:
            self.ips = {ip.strip() for ip in nowake_str.split(",") if ip.strip()}

    def should_skip_wol(self, ip_str):
        if not self.enabled:
            return False
        return ip_str in self.ips


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
                threading.Thread(
                    target=ha_notify,
                    args=("Plex WoL: Connection flood", msg),
                    daemon=True,
                ).start()


# ---------------------------------------------------------------------------
# Burst detection for smart WoL
# ---------------------------------------------------------------------------
class BurstDetector:
    """Tracks connection bursts to distinguish cold opens from background polls.

    A cold app open generates many connections in quick succession (loading
    providers, hubs, libraries, thumbnails). A background poll is a single
    connection every few minutes. When enabled, WoL is only sent if a burst
    of connections is detected within the configured window.
    """

    def __init__(self, enabled, burst_count, burst_window):
        self.enabled = enabled
        self.burst_count = max(2, burst_count)
        self.burst_window = burst_window
        self.timestamps = []
        self.lock = threading.Lock()
        # Once a burst triggers WoL, suppress further checks until
        # the server is confirmed up or the wake times out.
        self.wol_sent_recently = False
        self.wol_sent_time = 0.0
        self.wol_sent_cooldown = 120  # match wake_timeout

    def record_and_check(self):
        """Record a connection attempt while server is down.
        Returns True if burst threshold is met and WoL should be sent."""
        if not self.enabled:
            return True  # smart wol disabled, always send

        now = time.time()
        with self.lock:
            # If we already sent WoL recently, allow through (so clients
            # can wait for the server alongside the first burst)
            if self.wol_sent_recently and (now - self.wol_sent_time) < self.wol_sent_cooldown:
                return True

            self.timestamps = [t for t in self.timestamps if now - t < self.burst_window]
            self.timestamps.append(now)
            count = len(self.timestamps)

            if count >= self.burst_count:
                self.wol_sent_recently = True
                self.wol_sent_time = now
                return True

            return False

    def reset(self):
        """Reset burst state when server comes up."""
        with self.lock:
            self.timestamps = []
            self.wol_sent_recently = False

    def get_count(self):
        """Get current connection count in window (for logging)."""
        now = time.time()
        with self.lock:
            self.timestamps = [t for t in self.timestamps if now - t < self.burst_window]
            return len(self.timestamps)


# ---------------------------------------------------------------------------
# Sleep trigger
# ---------------------------------------------------------------------------
class SleepTrigger:
    def __init__(self, enabled, idle_minutes, max_awake_minutes, server_ip, ssh_user, ssh_port, toggles=None):
        self.base_enabled = enabled and (idle_minutes > 0 or max_awake_minutes > 0) and bool(ssh_user)
        self.toggles = toggles
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
        if self.toggles:
            return self.base_enabled and self.toggles.get_sleep()
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
                threading.Thread(
                    target=ha_notify,
                    args=("Plex WoL: Server sleeping",
                          f"Plex server {self.server_ip} put to sleep after idle timeout."),
                    daemon=True,
                ).start()
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
def start_health_check(enabled, port, sensors, server_ip, server_port, conn_history):
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

    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", port))
        except OSError as e:
            log(f"Health check: failed to bind port {port}: {e}")
            return
        srv.listen(5)
        log(f"Health check: listening on port {port}")
        while True:
            conn, _ = srv.accept()
            try:
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
                    "unique_ips_today": history,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                body = json.dumps(status, indent=2)
                response = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )
                conn.sendall(response.encode())
            except Exception:
                pass
            finally:
                conn.close()

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
        "enable_sleep_trigger": False,
        "sleep_idle_minutes": 0,
        "max_awake_minutes": 0,
        "server_ssh_user": "",
        "server_ssh_port": 22,
        "enable_health_check": True,
        "health_check_port": 32401,
        "enable_ha_sensors": True,
        "enable_file_logging": True,
        "log_retention_days": 14,
        "enable_quiet_mode": False,
        "user_friendly_names": "",
        "enable_smart_wol": False,
        "smart_wol_burst_count": 3,
        "smart_wol_burst_window": 15,
        "enable_user_tracking": False,
        "plex_admin_token": "",
        "enable_dashboard_toggles": True,
    }
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            file_opts = json.load(f) or {}
        defaults.update({k: v for k, v in file_opts.items() if v is not None and v != ""})
    except Exception as e:
        print(f"WARNING: Could not read {OPTIONS_PATH}: {e}. Using defaults.", flush=True)
    return defaults


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
    sockets = [client_sock, upstream_sock]
    try:
        while True:
            readable, _, errored = select.select(sockets, [], sockets, 30)
            if errored:
                break
            for s in readable:
                other = upstream_sock if s is client_sock else client_sock
                try:
                    data = s.recv(PROXY_BUF)
                except Exception:
                    data = b""
                if not data:
                    return
                try:
                    other.sendall(data)
                except Exception:
                    return
    finally:
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass


def wait_for_server(ip, port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            probe = socket.create_connection((ip, port), timeout=2)
            probe.close()
            return True
        except (OSError, ConnectionRefusedError):
            time.sleep(CONNECT_POLL_INTERVAL)
    return False


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
                  sensors, sleeper, toggles, session_tracker, conn_history, burst_detector, nowake_list, name_map):
    # Track connection
    connection_tracker.add(client_sock)

    try:
        _handle_client_inner(client_sock, addr, opts, wol_state, flood, geoip,
                             allowlist, blocklist, sensors, sleeper, toggles, session_tracker,
                             conn_history, burst_detector, nowake_list, name_map)
    finally:
        connection_tracker.remove(client_sock)


def _handle_client_inner(client_sock, addr, opts, wol_state, flood, geoip,
                         allowlist, blocklist, sensors, sleeper, toggles, session_tracker,
                         conn_history, burst_detector, nowake_list, name_map):
    # Flood detection
    flood.record(addr)

    # Sensor: count connection
    sensors.increment_connections()

    client_ip = addr[0]

    # Connection history
    conn_history.record(client_ip)

    # Quiet mode — check toggle or config
    quiet = toggles.get_quiet() if toggles.enabled else bool(opts.get("enable_quiet_mode", False))

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
        log(f"[{addr}] No plex_server_ip configured — dropping connection.")
        client_sock.close()
        return

    # Touch sleep trigger on every connection
    if sleeper:
        sleeper.touch(client_ip)

    user_tag = ""

    # Check if the real server is already up
    server_already_up = False
    try:
        probe = socket.create_connection((server_ip, server_port), timeout=2)
        probe.close()
        server_already_up = True
    except (OSError, ConnectionRefusedError):
        pass

    if server_already_up:
        if not quiet:
            log(f"[{addr}]{user_tag} Server already up — proxying to {server_ip}:{server_port}")
        sensors.set_server_status("up")
        burst_detector.reset()
    else:
        # No-wake list: proxy when up, drop when down
        if nowake_list.should_skip_wol(client_ip):
            if not quiet:
                log(f"[{addr}] Server down — no-wake list, dropping connection")
            client_sock.close()
            return

        # Check if WoL is enabled via dashboard toggle
        wol_config_enabled = bool(opts.get("enable_wol", True))
        wol_dashboard_enabled = toggles.get_wol() if toggles.enabled else True
        wol_enabled = wol_config_enabled and wol_dashboard_enabled

        # Smart WoL: check for connection burst before sending WoL
        burst_ok = burst_detector.record_and_check()

        sensors.set_server_status("waking")
        now = time.time()
        with wol_state["lock"]:
            elapsed = now - wol_state["last_wol"]
            if not wol_enabled:
                reason = "config" if not wol_config_enabled else "dashboard toggle"
                log(f"[{addr}]{user_tag} Server down — WoL disabled via {reason}")
            elif not burst_ok:
                count = burst_detector.get_count()
                log(f"[{addr}]{user_tag} Server down — background poll ignored by smart WoL "
                    f"({count}/{burst_detector.burst_count} connections in window)")
                client_sock.close()
                return
            elif target_mac and elapsed >= rate_limit:
                log(f"[{addr}]{user_tag} Server down — burst detected, sending WoL to {target_mac} via {broadcast}")
                try:
                    send_wol(target_mac, broadcast)
                    wol_state["last_wol"] = now
                    sensors.set_last_wake(None)
                except Exception as e:
                    log(f"[{addr}]{user_tag} ERROR sending WoL: {e}")
            elif not target_mac:
                log(f"[{addr}]{user_tag} Server down and no target_mac set — cannot send WoL")
            else:
                log(f"[{addr}]{user_tag} Server down — WoL rate-limited ({rate_limit - elapsed:.0f}s left)")

        log(f"[{addr}]{user_tag} Waiting up to {wake_timeout}s for server to come up…")
        if not wait_for_server(server_ip, server_port, wake_timeout):
            msg = (f"Plex server {server_ip} did not respond within "
                   f"{wake_timeout}s after WoL. Connection from {client_ip}{user_tag} dropped.")
            log(f"[{addr}]{user_tag} TIMEOUT: {msg}")
            sensors.set_server_status("timeout")
            threading.Thread(
                target=ha_notify,
                args=("Plex WoL: Wake failed", msg),
                daemon=True,
            ).start()
            client_sock.close()
            return
        log(f"[{addr}]{user_tag} Server is up — proxying.")
        sensors.set_server_status("up")
        burst_detector.reset()

        # Track who woke the server via session query
        if session_tracker.enabled:
            def _track_wake_user():
                wake_user = session_tracker.get_wake_user(delay=10)
                if wake_user:
                    display = get_friendly_name(wake_user, name_map)
                    log(f"[{addr}] Wake triggered by user: {display}")
                    sensors.set_last_wake(display)
                    threading.Thread(
                        target=ha_notify,
                        args=("Plex WoL: Server woken", f"Server woken by {display} from {client_ip}"),
                        daemon=True,
                    ).start()
            threading.Thread(target=_track_wake_user, daemon=True).start()

    try:
        upstream = socket.create_connection((server_ip, server_port), timeout=10)
    except Exception as e:
        log(f"[{addr}] Failed to connect to {server_ip}:{server_port}: {e}")
        client_sock.close()
        return

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

    log("=== Plex WoL Proxy v5.2 ===")
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
    log(f"  Sleep trigger:    {'ON — idle:' + str(opts.get('sleep_idle_minutes', 0)) + 'min, max awake:' + str(opts.get('max_awake_minutes', 0)) + 'min' if opts.get('enable_sleep_trigger') else 'OFF'}")
    log(f"  Health check:     {'ON — port ' + str(opts.get('health_check_port', 32401)) if opts.get('enable_health_check') else 'OFF'}")
    log(f"  HA sensors:       {'ON' if opts.get('enable_ha_sensors') else 'OFF'}")
    log(f"  Dashboard toggles:{'ON' if opts.get('enable_dashboard_toggles') else 'OFF'}")
    log(f"  File logging:     {'ON — ' + str(opts.get('log_retention_days', 14)) + ' day retention' if opts.get('enable_file_logging') else 'OFF'}")
    log(f"  Quiet mode:       {'ON' if opts.get('enable_quiet_mode') else 'OFF'}")
    log(f"  HA API token:     {'present' if SUPERVISOR_TOKEN else 'MISSING'}")

    if not opts.get("plex_server_ip"):
        log("*** WARNING: plex_server_ip is not set. The add-on won't do anything useful. ***")

    # Initialize all modules
    wol_state = {"last_wol": 0.0, "lock": threading.Lock()}
    plex_server_ip = str(opts.get("plex_server_ip", ""))
    flood_excluded = {plex_server_ip} if plex_server_ip else set()
    flood = FloodDetector(flood_threshold, flood_window, excluded_ips=flood_excluded)

    # Dashboard toggles (must init before geoip/sleeper so they can reference it)
    toggles = DashboardToggles(
        enabled=bool(opts.get("enable_dashboard_toggles", True)),
        initial_wol=bool(opts.get("enable_wol", True)),
        initial_geoip=bool(opts.get("enable_geoip", True)),
        initial_quiet=bool(opts.get("enable_quiet_mode", False)),
        initial_sleep=bool(opts.get("enable_sleep_trigger", False)),
    )

    geoip = GeoIPChecker(
        bool(opts.get("enable_geoip", True)),
        str(opts.get("allowed_countries", "US")),
        toggles=toggles,
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
    )

    sensors = HASensors(bool(opts.get("enable_ha_sensors", True)))
    sensors.set_server_status("unknown")
    sensors.publish_all()

    conn_history = ConnectionHistory(bool(opts.get("enable_ha_sensors", True)))

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
        toggles=toggles,
    )

    start_health_check(
        bool(opts.get("enable_health_check", True)),
        int(opts.get("health_check_port", 32401)),
        sensors,
        str(opts.get("plex_server_ip", "")),
        int(opts.get("plex_server_port", 32400)),
        conn_history,
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
                  sensors, sleeper, toggles, session_tracker, conn_history, burst_detector, nowake_list, name_map),
            daemon=True,
        )
        t.start()


if __name__ == "__main__":
    main()
