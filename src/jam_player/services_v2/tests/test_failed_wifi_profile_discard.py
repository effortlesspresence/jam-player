"""
A WiFi attempt that fails to ACTIVATE (wrong password, AP rejected us) must not
leave its NetworkManager profile behind.

_connect_wifi_secure persists the keyfile (autoconnect=true, with the psk)
BEFORE it knows whether the credentials work. On fielded 7440d2d a failed
`nmcli connection up` returned non-zero (not an exception), so the only cleanup
-- in the except block -- never ran and the bad-password profile stayed on disk.
NetworkManager then kept retrying that password on its own and again every
reboot: the "entered a wrong password and the JP kept saving and retrying it"
bug. Runs on a JAM Player (real modules); os.write / subprocess / nmcli are
mocked so NetworkManager is never touched.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402

UP = ['nmcli', 'connection', 'up']
DELETE = ['nmcli', 'connection', 'delete']


def _fake_nmcli(up_returncode, calls):
    """Fake subprocess.run: records every call. `connection up` returns the
    given code (with the stderr NM emits for a wrong PSK); everything else
    (show / reload / delete) succeeds with empty output, so the pre-connect
    same-SSID sweep finds nothing and issues no deletes of its own."""
    def run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        rc = up_returncode if list(cmd[:3]) == UP else 0
        stderr = 'Secrets were required, but not provided' if rc else ''
        return mock.MagicMock(returncode=rc, stdout='', stderr=stderr)
    return run


class FailedProfileDiscardTests(unittest.TestCase):

    def _attempt(self, up_returncode):
        calls = []
        with mock.patch.object(network.subprocess, "run",
                               side_effect=_fake_nmcli(up_returncode, calls)), \
             mock.patch.object(network.os, "open", return_value=7), \
             mock.patch.object(network.os, "write", side_effect=lambda fd, d: len(d)), \
             mock.patch.object(network.os, "close"), \
             mock.patch.object(network.os, "chown"), \
             mock.patch.object(network.Path, "unlink"):
            result = network._connect_wifi_secure("MyNet", "wrong-password")
        return result, calls

    def _conn_name(self, calls):
        ups = [c for c in calls if c[:3] == UP]
        self.assertEqual(len(ups), 1, calls)
        return ups[0][3]

    def _deletes_of(self, calls, name):
        return [c for c in calls if c[:3] == DELETE and c[3] == name]

    def test_failed_activation_deletes_the_just_created_profile(self):
        result, calls = self._attempt(up_returncode=4)
        self.assertNotEqual(result.returncode, 0)
        name = self._conn_name(calls)
        self.assertTrue(name.startswith('jam-wifi-'), name)
        self.assertEqual(len(self._deletes_of(calls, name)), 1,
                         'failed profile must be deleted so NM cannot keep retrying the bad password')

    def test_delete_happens_after_the_failed_up_not_before(self):
        """The pre-connect sweep deletes OTHER same-SSID profiles before `up`;
        the fix must delete THIS attempt's profile after `up` fails."""
        _, calls = self._attempt(up_returncode=4)
        name = self._conn_name(calls)
        up_i = next(i for i, c in enumerate(calls) if c[:3] == UP)
        del_i = next(i for i, c in enumerate(calls) if c[:3] == DELETE and c[3] == name)
        self.assertGreater(del_i, up_i)

    def test_successful_activation_keeps_the_profile(self):
        """A profile that actually authenticated is the one worth persisting."""
        result, calls = self._attempt(up_returncode=0)
        self.assertEqual(result.returncode, 0)
        name = self._conn_name(calls)
        self.assertEqual(self._deletes_of(calls, name), [],
                         'a profile that authenticated must NOT be deleted')

    def test_cleanup_does_not_mask_the_failure(self):
        """connect_to_wifi maps this stderr to 'Invalid password' -- the
        discard must leave the failed result intact for it."""
        result, _ = self._attempt(up_returncode=4)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Secrets were required', result.stderr)

    def test_discard_never_raises_even_if_nmcli_is_gone(self):
        """Best-effort: a broken nmcli must not turn a clean failure into a crash."""
        with mock.patch.object(network.subprocess, "run", side_effect=OSError("nmcli gone")), \
             mock.patch.object(network.Path, "exists", return_value=False):
            network._discard_failed_wifi_profile(
                'jam-wifi-deadbeef', Path('/nonexistent/jam-wifi-deadbeef.nmconnection'))

    def test_discard_falls_back_to_unlink_and_reload_when_nmcli_delete_leaves_the_file(self):
        """If `nmcli connection delete` didn't remove the keyfile (NM never
        loaded it), unlink it ourselves and reload so no stale profile lingers."""
        calls = []
        def run(cmd, *a, **k):
            calls.append(list(cmd))
            return mock.MagicMock(returncode=1, stdout='', stderr='unknown connection')
        with mock.patch.object(network.subprocess, "run", side_effect=run), \
             mock.patch.object(network.Path, "exists", return_value=True), \
             mock.patch.object(network.Path, "unlink") as unlink:
            network._discard_failed_wifi_profile(
                'jam-wifi-cafe0001', Path('/x/jam-wifi-cafe0001.nmconnection'))
        unlink.assert_called_once()
        self.assertIn(['nmcli', 'connection', 'reload'], calls)


if __name__ == "__main__":
    unittest.main()
