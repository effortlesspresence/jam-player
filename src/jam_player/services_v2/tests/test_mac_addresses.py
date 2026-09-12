"""
Reading + reporting the device's permanent WiFi/ethernet MACs.

We pin cloned-mac-address=permanent, so the current hwaddr IS permanent.
MACs never change, so they're reported once after announce and once per boot
(first successful heartbeat), never on an interval. Runs on a JAM Player
(real modules); nmcli/HTTP are mocked.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


def _devshow(*blocks):
    """Build fake `nmcli -t -f GENERAL.TYPE,GENERAL.HWADDR device show` output.
    Each block is (type, hwaddr); nmcli escapes ':' in the MAC as '\\:'."""
    lines = []
    for t, hw in blocks:
        lines.append(f"GENERAL.TYPE:{t}")
        lines.append("GENERAL.HWADDR:" + (hw.replace(":", "\\:") if hw else ""))
    return mock.MagicMock(returncode=0, stdout="\n".join(lines) + "\n", stderr="")


class GetMacAddressesTests(unittest.TestCase):
    def test_reads_both_wifi_and_ethernet_uppercased(self):
        with mock.patch.object(network.subprocess, "run",
                               return_value=_devshow(("wifi", "2c:cf:67:ab:cd:ef"),
                                                     ("ethernet", "2c:cf:67:11:22:33"))):
            self.assertEqual(network.get_mac_addresses(),
                             {"wifiMac": "2C:CF:67:AB:CD:EF", "ethernetMac": "2C:CF:67:11:22:33"})

    def test_wifi_only_device_reports_null_ethernet(self):
        with mock.patch.object(network.subprocess, "run",
                               return_value=_devshow(("wifi", "2c:cf:67:ab:cd:ef"),
                                                     ("loopback", ""))):
            self.assertEqual(network.get_mac_addresses(),
                             {"wifiMac": "2C:CF:67:AB:CD:EF", "ethernetMac": None})

    def test_nmcli_failure_returns_both_none(self):
        with mock.patch.object(network.subprocess, "run", side_effect=OSError("no nmcli")):
            self.assertEqual(network.get_mac_addresses(), {"wifiMac": None, "ethernetMac": None})


class ReportMacAddressesTests(unittest.TestCase):
    def _ok(self, status=200):
        return mock.MagicMock(status_code=status)

    def test_posts_both_macs_to_the_right_endpoint(self):
        sent = {}
        def fake(method, path, body, signed):
            sent.update(method=method, path=path, body=body, signed=signed)
            return self._ok(200)
        with mock.patch.object(network, "get_mac_addresses",
                               return_value={"wifiMac": "AA:BB:CC:DD:EE:01", "ethernetMac": "AA:BB:CC:DD:EE:02"}), \
             mock.patch("common.api.api_request", side_effect=fake):
            self.assertTrue(network.report_mac_addresses())
        self.assertEqual(sent["path"], "/jam-players/mac-addresses")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["body"], {"wifiMac": "AA:BB:CC:DD:EE:01", "ethernetMac": "AA:BB:CC:DD:EE:02"})
        self.assertTrue(sent["signed"])

    def test_skips_the_call_entirely_when_no_mac_is_readable(self):
        with mock.patch.object(network, "get_mac_addresses",
                               return_value={"wifiMac": None, "ethernetMac": None}), \
             mock.patch("common.api.api_request", side_effect=AssertionError("must not POST")):
            self.assertFalse(network.report_mac_addresses())

    def test_non_2xx_returns_false(self):
        with mock.patch.object(network, "get_mac_addresses",
                               return_value={"wifiMac": "AA:BB:CC:DD:EE:01", "ethernetMac": None}), \
             mock.patch("common.api.api_request", return_value=self._ok(404)):
            self.assertFalse(network.report_mac_addresses())

    def test_never_raises_on_post_failure(self):
        with mock.patch.object(network, "get_mac_addresses",
                               return_value={"wifiMac": "AA:BB:CC:DD:EE:01", "ethernetMac": None}), \
             mock.patch("common.api.api_request", side_effect=OSError("down")):
            self.assertFalse(network.report_mac_addresses())


if __name__ == "__main__":
    unittest.main()
