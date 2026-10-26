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


if __name__ == "__main__":
    unittest.main()
