"""
Hidden-network support: a manually-typed SSID connects because the profile
carries hidden=true, which makes NetworkManager actively probe for the name.
The flag is additive -- an app that omits it (every fielded build) gets the
exact visible-network keyfile as before. Runs on a JAM Player (real modules);
NetworkManager is never touched (os.write / subprocess / nmcli are mocked).
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


class HiddenKeyfileTests(unittest.TestCase):
    def _written_keyfile(self, hidden):
        """Capture the exact bytes _connect_wifi_secure writes to the profile."""
        captured = {}
        ok = mock.MagicMock(returncode=0, stdout="", stderr="")

        def fake_write(fd, data):
            captured["text"] = data.decode("utf-8")
            return len(data)

        with mock.patch.object(network.subprocess, "run", return_value=ok), \
             mock.patch.object(network.os, "open", return_value=7), \
             mock.patch.object(network.os, "write", side_effect=fake_write), \
             mock.patch.object(network.os, "close"), \
             mock.patch.object(network.os, "chown"), \
             mock.patch.object(network.Path, "unlink"):
            network._connect_wifi_secure("MyNet", "pw12345", hidden=hidden)
        return captured.get("text", "")

    def test_hidden_true_writes_the_probe_flag_in_the_wifi_section(self):
        text = self._written_keyfile(hidden=True)
        self.assertIn("hidden=true", text)
        # It belongs to [wifi], before the security/ipv4 sections.
        self.assertLess(text.index("mode=infrastructure"), text.index("hidden=true"))
        self.assertLess(text.index("hidden=true"), text.index("[ipv4]"))
        self.assertIn("ssid=MyNet", text)

    def test_hidden_false_keyfile_has_no_hidden_line(self):
        text = self._written_keyfile(hidden=False)
        self.assertNotIn("hidden", text)
        self.assertIn("mode=infrastructure", text)
        self.assertIn("ssid=MyNet", text)

    def test_connect_to_wifi_defaults_to_visible(self):
        """Every path except manual entry omits hidden -> unchanged behavior."""
        with mock.patch.object(network, "_get_active_wifi_connection", return_value=None), \
             mock.patch.object(network, "_log_network_diagnostic_info"), \
             mock.patch.object(network, "_stop_comitup_hotspot", return_value=False), \
             mock.patch.object(network, "_connect_wifi_secure",
                               return_value=mock.MagicMock(returncode=0)) as secure, \
             mock.patch.object(network, "_promote_in_background"):
            network.connect_to_wifi("Vis", "pw")
        self.assertFalse(secure.call_args.kwargs.get("hidden", False))

    def test_connect_to_wifi_forwards_hidden(self):
        with mock.patch.object(network, "_get_active_wifi_connection", return_value=None), \
             mock.patch.object(network, "_log_network_diagnostic_info"), \
             mock.patch.object(network, "_stop_comitup_hotspot", return_value=False), \
             mock.patch.object(network, "_connect_wifi_secure",
                               return_value=mock.MagicMock(returncode=0)) as secure, \
             mock.patch.object(network, "_promote_in_background"):
            network.connect_to_wifi("Hidden", "pw", hidden=True)
        self.assertTrue(secure.call_args.kwargs.get("hidden"))

    def test_open_hidden_network_has_no_security_but_keeps_hidden(self):
        """Manual entry with a blank password: open network, still probed."""
        text_open = None
        ok = mock.MagicMock(returncode=0, stdout="", stderr="")
        cap = {}
        with mock.patch.object(network.subprocess, "run", return_value=ok), \
             mock.patch.object(network.os, "open", return_value=7), \
             mock.patch.object(network.os, "write", side_effect=lambda fd, d: cap.__setitem__("t", d.decode()) or len(d)), \
             mock.patch.object(network.os, "close"), \
             mock.patch.object(network.os, "chown"), \
             mock.patch.object(network.Path, "unlink"):
            network._connect_wifi_secure("OpenNet", "", hidden=True)
        text_open = cap["t"]
        self.assertIn("hidden=true", text_open)
        self.assertNotIn("wpa-psk", text_open)  # no password -> no security section


if __name__ == "__main__":
    unittest.main()
