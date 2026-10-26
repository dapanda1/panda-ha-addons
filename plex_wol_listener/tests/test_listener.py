"""Tests for the Plex WoL listener.

Run from the add-on directory:  python3 -m unittest discover -s tests
"""

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import listener as L  # noqa: E402

PUBLIC_IP = "73.15.22.9"  # a real, globally routable address
CLOSED_PORT = 1  # nothing listens here, so the "server" reads as down


class TempDataDir(unittest.TestCase):
    """Points every file the listener persists at a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patches = [
            mock.patch.object(L.NoWakeList, "LEARNED_FILE", os.path.join(self.tmp, "learned.json")),
            mock.patch.object(L.ToggleManager, "TOGGLES_FILE", os.path.join(self.tmp, "toggles.json")),
            mock.patch.object(L, "OPTIONS_PATH", os.path.join(self.tmp, "options.json")),
            mock.patch.object(L, "log", lambda msg: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        with open(L.OPTIONS_PATH, "w") as f:
            f.write("{}")

    def make_env(self, **overrides):
        """Build the full set of handler dependencies with everything optional turned off."""
        toggles = L.ToggleManager(True, False, True, False)
        env = {
            "opts": dict(L.load_options(), **{
                "plex_server_ip": "127.0.0.1",
                "plex_server_port": CLOSED_PORT,
                "target_mac": "aa:bb:cc:dd:ee:ff",
                "broadcast": "127.255.255.255",
                "wake_timeout_seconds": 1,
            }),
            "wol_state": {"last_wol": 0.0, "lock": threading.Lock()},
            "flood": L.FloodDetector(10 ** 9, 60),
            "geoip": L.GeoIPChecker(False, "US", toggles),
            "allowlist": L.IPAllowlist(False, ""),
            "blocklist": L.IPBlocklist(False, ""),
            "sensors": L.HASensors(False),
            "sleeper": None,
            "toggle_manager": toggles,
            "session_tracker": L.PlexSessionTracker(False, "", 0, ""),
            "conn_history": L.ConnectionHistory(False),
            "burst_detector": L.BurstDetector(False, 3, 15),
            "nowake_list": L.NoWakeList(False, ""),
            "name_map": {},
        }
        env.update(overrides)
        return env

    def connect(self, env, ip):
        """Run one client connection from `ip` through the handler."""
        client, peer = socket.socketpair()
        self.addCleanup(peer.close)
        L._handle_client_inner(client, (ip, 40000), **env)


class SmartWolLearningTest(TempDataDir):
    """Bug: the first connection of a real user's burst got their IP learned as
    no-wake, so the rest of the burst was dropped and the server never woke."""

    def setUp(self):
        super().setUp()
        self.burst = L.BurstDetector(True, 3, 15)
        self.nowake = L.NoWakeList(True, "")
        self.env = self.make_env(burst_detector=self.burst, nowake_list=self.nowake)

    def test_remote_burst_reaches_threshold_and_is_not_learned(self):
        with mock.patch.object(L, "send_wol") as send_wol:
            for _ in range(3):
                self.connect(self.env, PUBLIC_IP)
        send_wol.assert_called_once()
        self.assertFalse(self.nowake.should_skip_wol(PUBLIC_IP))

    def test_lone_probe_is_learned_after_its_window(self):
        with mock.patch.object(L, "send_wol") as send_wol:
            self.connect(self.env, PUBLIC_IP)
            self.assertFalse(self.nowake.should_skip_wol(PUBLIC_IP))
            # Let the window pass, then any later server-down connection triggers learning
            self.burst.single_probes[PUBLIC_IP] -= 16
            self.connect(self.env, "8.8.8.8")
        send_wol.assert_not_called()
        self.assertTrue(self.nowake.should_skip_wol(PUBLIC_IP))

    def test_burst_is_counted_per_ip(self):
        with mock.patch.object(L, "send_wol") as send_wol:
            for ip in ("8.8.8.8", "8.8.4.4", "1.1.1.1"):
                self.connect(self.env, ip)
        send_wol.assert_not_called()

    def test_reset_forgets_pending_probes(self):
        self.burst.record_and_check(PUBLIC_IP)
        self.burst.reset()
        self.burst.single_probes[PUBLIC_IP] = time.time() - 60  # would have expired
        self.burst.reset()
        self.assertEqual(self.burst.pop_single_probes(), [])


class ToggleConfigTest(TempDataDir):
    """Bug: once toggles.json existed, changing enable_geoip / enable_quiet_mode
    in the add-on config had no effect."""

    def test_dashboard_choice_survives_restart(self):
        L.ToggleManager(True, True, False, False).set("geoip", False)
        self.assertFalse(L.ToggleManager(True, True, False, False).get_geoip())

    def test_config_change_overrides_dashboard_choice(self):
        L.ToggleManager(True, True, False, False).set("geoip", False)
        # User turns GeoIP off in config, restarts, then back on and restarts
        self.assertFalse(L.ToggleManager(True, False, False, False).get_geoip())
        self.assertTrue(L.ToggleManager(True, True, False, False).get_geoip())

    def test_config_off_disables_geoip(self):
        L.ToggleManager(True, True, False, False)  # first run writes the file
        toggles = L.ToggleManager(True, False, False, False)
        self.assertFalse(L.GeoIPChecker(False, "US", toggles).enabled)

    def test_legacy_flat_file_keeps_dashboard_state(self):
        with open(L.ToggleManager.TOGGLES_FILE, "w") as f:
            f.write('{"wol": true, "geoip": false, "quiet": true, "sleep": false}')
        toggles = L.ToggleManager(True, True, False, False)
        self.assertFalse(toggles.get_geoip())
        self.assertTrue(toggles.get_quiet())
        # After the upgrade, config changes take effect: quiet config off -> on -> off
        self.assertTrue(L.ToggleManager(True, True, True, False).get_quiet())
        self.assertFalse(L.ToggleManager(True, True, False, False).get_quiet())


class NetworkOptsTest(unittest.TestCase):
    """Bug: the shipped default plex_server_ip 0.0.0.0 made the proxy connect to
    itself, spawning thousands of threads from a single client connection."""

    def sanitize(self, **opts):
        opts = dict({"plex_server_port": 32400, "listen_port": 32400,
                     "target_mac": "aa:bb:cc:dd:ee:ff", "broadcast": "192.168.1.255"}, **opts)
        problems = L.sanitize_network_opts(opts)
        return opts, problems

    def test_local_targets_on_same_port_are_refused(self):
        for ip in ("0.0.0.0", "127.0.0.1"):
            opts, problems = self.sanitize(plex_server_ip=ip)
            self.assertEqual(opts["plex_server_ip"], "", ip)
            self.assertTrue(problems)

    def test_local_target_on_other_port_is_allowed(self):
        opts, _ = self.sanitize(plex_server_ip="127.0.0.1", plex_server_port=32402)
        self.assertEqual(opts["plex_server_ip"], "127.0.0.1")

    def test_remote_target_is_kept(self):
        opts, problems = self.sanitize(plex_server_ip="192.0.2.10")
        self.assertEqual(opts["plex_server_ip"], "192.0.2.10")
        self.assertEqual(problems, [])

    def test_placeholder_mac_and_broadcast(self):
        opts, _ = self.sanitize(plex_server_ip="192.0.2.10", target_mac="00:00:00:00:00:00",
                                broadcast="0.0.0.0")
        self.assertEqual(opts["target_mac"], "")
        self.assertEqual(opts["broadcast"], "255.255.255.255")
        opts, _ = self.sanitize(plex_server_ip="192.0.2.10", target_mac="zz:bb:cc:dd:ee:ff")
        self.assertEqual(opts["target_mac"], "")


class ProxyDataTest(unittest.TestCase):
    """Bug: proxy_data used select(), which raises on file descriptors >= 1024."""

    def test_relays_with_high_file_descriptors(self):
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 1200:
            self.skipTest("fd limit too low to reach fd 1024")
        filler = [socket.socket() for _ in range(1050)]
        self.addCleanup(lambda: [s.close() for s in filler])
        client_side, client = socket.socketpair()
        upstream, upstream_side = socket.socketpair()
        self.assertGreaterEqual(upstream.fileno(), 1024)
        t = threading.Thread(target=L.proxy_data, args=(client, upstream), daemon=True)
        t.start()
        client_side.sendall(b"ping")
        self.assertEqual(upstream_side.recv(4), b"ping")
        upstream_side.sendall(b"pong")
        self.assertEqual(client_side.recv(4), b"pong")
        client_side.close()
        t.join(5)
        self.assertFalse(t.is_alive())
        upstream_side.close()


class FakeSupervisor:
    """A local stand-in for the Supervisor API: /addons/self/info and /addons/self/options."""

    def __init__(self, options):
        import http.server
        import json as _json
        fake = self
        self.options = options
        self.posted = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, data):
                body = _json.dumps({"result": "ok", "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._reply({"options": dict(fake.options)} if self.path == "/addons/self/info" else {})

            def do_POST(self):
                payload = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.posted.append((self.path, payload))
                if self.path == "/addons/self/options":
                    fake.options = payload["options"]
                self._reply({})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ConfigSyncTest(TempDataDir):
    """Bug: syncing learned IPs posted back /data/options.json, which is only
    written at container start, silently undoing config saved since."""

    def setUp(self):
        super().setUp()
        self.supervisor = FakeSupervisor({"plex_server_ip": "192.0.2.10", "ip_blocklist": "",
                                          "learned_nowake_ips": ""})
        self.addCleanup(self.supervisor.close)
        for p in (mock.patch.object(L, "SUPERVISOR_URL", self.supervisor.url),
                  mock.patch.object(L, "SUPERVISOR_TOKEN", "token"),
                  mock.patch.object(L, "ha_worker", L.BackgroundWorker())):
            p.start()
            self.addCleanup(p.stop)

    def test_sync_keeps_config_saved_since_start(self):
        with open(L.OPTIONS_PATH, "w") as f:  # what the container started with
            f.write('{"plex_server_ip": "192.0.2.10", "ip_blocklist": ""}')
        self.supervisor.options["ip_blocklist"] = "198.51.100.9"  # user saved this later
        nowake = L.NoWakeList(True, "")
        nowake.learn(PUBLIC_IP)
        nowake._sync_to_config()
        self.assertEqual(self.supervisor.options["ip_blocklist"], "198.51.100.9")
        self.assertEqual(self.supervisor.options["learned_nowake_ips"], PUBLIC_IP)

    def test_learn_only_queues_the_sync(self):
        nowake = L.NoWakeList(True, "")
        self.supervisor.posted.clear()
        self.assertTrue(nowake.learn(PUBLIC_IP))
        self.assertEqual(self.supervisor.posted, [])  # nothing sent on the connection path
        self.assertIn("config:learned_nowake_ips", L.ha_worker.keyed)

    def test_unchanged_options_are_not_posted(self):
        self.assertFalse(L.update_addon_options({"plex_server_ip": "192.0.2.10"}))
        self.assertEqual(self.supervisor.posted, [])


class BackgroundWorkerTest(unittest.TestCase):
    def test_keyed_jobs_collapse_to_latest_and_keyless_all_run(self):
        worker = L.BackgroundWorker()
        ran = []
        for i in range(5):
            worker.submit(lambda i=i: ran.append(("state", i)), key="state:x")
        worker.submit(lambda: ran.append("notify-1"))
        worker.submit(lambda: ran.append("notify-2"))
        done = threading.Event()
        worker.submit(done.set, key="zz-last")
        worker.start()
        self.assertTrue(done.wait(5))
        self.assertEqual(ran, ["notify-1", "notify-2", ("state", 4)])

    def test_min_interval_spaces_out_runs(self):
        worker = L.BackgroundWorker()
        worker.start()
        times = []
        second = threading.Event()
        worker.submit(lambda: times.append(time.monotonic()), key="k", min_interval=0.5)
        time.sleep(0.1)
        worker.submit(lambda: (times.append(time.monotonic()), second.set()), key="k", min_interval=0.5)
        self.assertTrue(second.wait(5))
        self.assertGreaterEqual(times[1] - times[0], 0.45)


class MigrateTest(unittest.TestCase):
    def setUp(self):
        import migrate
        self.migrate = migrate
        p = mock.patch("builtins.print")
        p.start()
        self.addCleanup(p.stop)

    def test_rename_moves_value(self):
        opts = {"nowake_exclude": "34.0.0.0/8", "allow_ip_plex_relay": "", "enable_token_validation": True}
        self.assertTrue(self.migrate.apply_field_map(opts))
        self.assertEqual(opts, {"allow_ip_plex_relay": "34.0.0.0/8"})

    def test_rename_keeps_value_already_set(self):
        opts = {"nowake_exclude": "old", "allow_ip_plex_relay": "new"}
        self.migrate.apply_field_map(opts)
        self.assertEqual(opts, {"allow_ip_plex_relay": "new"})

    def test_reads_saved_options_not_options_json(self):
        supervisor = FakeSupervisor({"nowake_exclude": "34.0.0.0/8", "allow_ip_plex_relay": ""})
        self.addCleanup(supervisor.close)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(self.migrate, "SUPERVISOR_URL", supervisor.url), \
                mock.patch.object(self.migrate, "SUPERVISOR_TOKEN", "token"), \
                mock.patch.object(self.migrate, "SCHEMA_VERSION_FILE", os.path.join(tmp, "v")):
            self.migrate.migrate()
        self.assertEqual(supervisor.options, {"allow_ip_plex_relay": "34.0.0.0/8"})
        with open(os.path.join(tmp, "v")) as f:
            self.assertEqual(f.read(), str(self.migrate.CURRENT_SCHEMA_VERSION))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HealthEndpointTest(TempDataDir):
    def setUp(self):
        super().setUp()
        self.toggles = L.ToggleManager(True, True, False, False)
        self.port = free_port()
        L.start_health_check(True, self.port, L.HASensors(False), "127.0.0.1", CLOSED_PORT,
                             L.ConnectionHistory(False), self.toggles)
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    def request(self, method, path, headers=None):
        """Send a raw request and return (status code, raw response text)."""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            while chunk := s.recv(4096):
                data += chunk
        text = data.decode()
        return int(text.split(" ")[1]), text

    def test_explicit_on_off(self):
        status, _ = self.request("POST", "/toggle/wol/off")
        self.assertEqual(status, 200)
        self.assertFalse(self.toggles.get_wol())
        status, _ = self.request("POST", "/toggle/wol/on")
        self.assertEqual(status, 200)
        self.assertTrue(self.toggles.get_wol())

    def test_flip_and_unknown(self):
        self.assertEqual(self.request("POST", "/toggle/quiet")[0], 200)
        self.assertTrue(self.toggles.get_quiet())
        self.assertEqual(self.request("POST", "/toggle/nope")[0], 400)
        self.assertEqual(self.request("POST", "/toggle/wol/maybe")[0], 404)

    def test_browser_post_is_refused(self):
        status, _ = self.request("POST", "/toggle/wol/off", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertTrue(self.toggles.get_wol())

    def test_no_wildcard_cors(self):
        status, text = self.request("GET", "/status?x=1")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", text)


if __name__ == "__main__":
    unittest.main()
