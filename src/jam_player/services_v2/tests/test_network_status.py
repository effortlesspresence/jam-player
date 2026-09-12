"""
get_reported_network_status(): what the device tells the backend it's on.

Ethernet wins the default route on our devices (lower metric) and carries no
SSID; only WiFi carries an SSID. Runs on a JAM Player (real modules); nmcli
is mocked so no real network is touched.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


def _nmcli(active_types):
    """Fake `nmcli -t -f TYPE connection show --active` output."""
    cp = mock.MagicMock(returncode=0, stdout="\n".join(active_types) + "\n", stderr="")
    return cp


class ReportedNetworkStatusTests(unittest.TestCase):
    def test_wifi_only_reports_wifi_with_ssid(self):
        with mock.patch.object(network.subprocess, "run", return_value=_nmcli(["802-11-wireless"])), \
             mock.patch.object(network, "_get_active_wifi_connection",
                               return_value={"name": "jam-wifi-1", "ssid": "HomeNet"}):
            self.assertEqual(network.get_reported_network_status(),
                             {"connectionType": "wifi", "ssid": "HomeNet"})

    def test_ethernet_only_reports_ethernet_no_ssid(self):
        with mock.patch.object(network.subprocess, "run", return_value=_nmcli(["802-3-ethernet"])):
            self.assertEqual(network.get_reported_network_status(),
                             {"connectionType": "ethernet", "ssid": None})

    def test_both_active_ethernet_wins(self):
        # Ethernet carries the default route -> that's what the device uses.
        with mock.patch.object(network.subprocess, "run",
                               return_value=_nmcli(["802-11-wireless", "802-3-ethernet"])), \
             mock.patch.object(network, "_get_active_wifi_connection",
                               return_value={"name": "w", "ssid": "HomeNet"}):
            self.assertEqual(network.get_reported_network_status(),
                             {"connectionType": "ethernet", "ssid": None})

    def test_nothing_active_reports_other(self):
        with mock.patch.object(network.subprocess, "run", return_value=_nmcli([])):
            self.assertEqual(network.get_reported_network_status(),
                             {"connectionType": "other", "ssid": None})

    def test_nmcli_failure_never_raises(self):
        with mock.patch.object(network.subprocess, "run", side_effect=OSError("no nmcli")):
            self.assertEqual(network.get_reported_network_status(),
                             {"connectionType": "other", "ssid": None})

    def test_wifi_active_but_ssid_unreadable_falls_back_to_other(self):
        with mock.patch.object(network.subprocess, "run", return_value=_nmcli(["802-11-wireless"])), \
             mock.patch.object(network, "_get_active_wifi_connection", return_value=None):
            self.assertEqual(network.get_reported_network_status(),
                             {"connectionType": "other", "ssid": None})


class ReportNetworkStatusTests(unittest.TestCase):
    """The shared reporter used by BOTH jam-heartbeat (periodic) and
    jam-announce (once, right after announce). POSTs the current network;
    best-effort and never raises into its caller."""

    def _ok(self, status=200):
        return mock.MagicMock(status_code=status)

    def test_posts_current_network_to_the_right_endpoint(self):
        sent = {}
        def fake_api_request(method, path, body, signed):
            sent.update(method=method, path=path, body=body, signed=signed)
            return self._ok(200)
        with mock.patch.object(network, "get_reported_network_status",
                               return_value={"connectionType": "wifi", "ssid": "HomeNet"}), \
             mock.patch("common.api.api_request", side_effect=fake_api_request):
            self.assertTrue(network.report_network_status())
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "/jam-players/network-status")
        self.assertEqual(sent["body"], {"connectionType": "wifi", "ssid": "HomeNet"})
        self.assertTrue(sent["signed"])

    def test_non_2xx_returns_false(self):
        with mock.patch.object(network, "get_reported_network_status",
                               return_value={"connectionType": "ethernet", "ssid": None}), \
             mock.patch("common.api.api_request", return_value=self._ok(404)):
            self.assertFalse(network.report_network_status())

    def test_never_raises_when_the_post_fails(self):
        with mock.patch.object(network, "get_reported_network_status",
                               return_value={"connectionType": "wifi", "ssid": "X"}), \
             mock.patch("common.api.api_request", side_effect=OSError("network down")):
            self.assertFalse(network.report_network_status())  # swallowed, no raise


if __name__ == "__main__":
    unittest.main()
