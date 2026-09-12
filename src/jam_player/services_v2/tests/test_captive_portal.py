"""
Captive-portal / dead-network detection in the BLE connect flow.

WiFi association is not usable internet. After a successful associate the
handler verifies real internet; if that fails (captive portal, firewall,
dead uplink) it drops the profile and reports the network as unusable with
a dual status (no_internet -> failed) so new apps get a specific message
and old apps still fail gracefully. On-device only (real modules).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


class ForgetActiveProfileTests(unittest.TestCase):
    def test_forgets_the_active_wifi_profile(self):
        ok = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(network, "_get_active_wifi_connection",
                               return_value={"name": "jam-wifi-dead", "ssid": "Cafe"}), \
             mock.patch.object(network.subprocess, "run", return_value=ok) as run:
            self.assertTrue(network.forget_active_wifi_connection())
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["nmcli", "connection", "delete"])
        self.assertIn("jam-wifi-dead", argv)

    def test_no_active_profile_is_a_noop(self):
        with mock.patch.object(network, "_get_active_wifi_connection", return_value=None), \
             mock.patch.object(network.subprocess, "run",
                               side_effect=AssertionError("must not call nmcli")):
            self.assertFalse(network.forget_active_wifi_connection())

    def test_never_raises_on_error(self):
        with mock.patch.object(network, "_get_active_wifi_connection",
                               side_effect=OSError("boom")):
            self.assertFalse(network.forget_active_wifi_connection())


class ClassifyConnectivityTests(unittest.TestCase):
    """
    The connect flow must sort a network into exactly one of three buckets,
    and never mislabel a GOOD network. Each phase retries so a settling
    connection is not misjudged.
    """

    def test_backend_reachable_is_backend_no_retry_cost(self):
        with mock.patch.object(network, "check_api_availability", return_value=True) as api, \
             mock.patch.object(network, "_check_tls_connectivity") as tls, \
             mock.patch.object(network.time, "sleep") as slept:
            self.assertEqual(network.classify_connectivity(), "backend")
        api.assert_called_once()          # good network pays no retry cost
        tls.assert_not_called()           # never even probes the fallbacks
        slept.assert_not_called()

    def test_a_transient_backend_miss_then_success_is_still_backend(self):
        # Backend fails the first probe (settling) then succeeds: must NOT be
        # branded a firewall.
        api_results = iter([False, True])
        with mock.patch.object(network, "check_api_availability",
                               side_effect=lambda *a, **k: next(api_results)), \
             mock.patch.object(network, "_check_tls_connectivity") as tls, \
             mock.patch.object(network.time, "sleep"):
            self.assertEqual(network.classify_connectivity(attempts=3, delay=0), "backend")
        tls.assert_not_called()

    def test_internet_but_no_backend_is_internet_only(self):
        # Backend never reachable (firewall or our outage); public TLS works.
        with mock.patch.object(network, "check_api_availability", return_value=False) as api, \
             mock.patch.object(network, "_check_tls_connectivity", return_value=True), \
             mock.patch.object(network.time, "sleep"):
            self.assertEqual(network.classify_connectivity(attempts=3, delay=0), "internet_only")
        self.assertEqual(api.call_count, 3)   # backend fully retried before concluding

    def test_nothing_reachable_is_none(self):
        with mock.patch.object(network, "check_api_availability", return_value=False), \
             mock.patch.object(network, "_check_tls_connectivity", return_value=False), \
             mock.patch.object(network.time, "sleep"):
            self.assertEqual(network.classify_connectivity(attempts=3, delay=0), "none")



if __name__ == "__main__":
    unittest.main()
