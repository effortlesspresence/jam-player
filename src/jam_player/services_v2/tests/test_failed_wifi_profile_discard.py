"""
WiFi connect attempts must never destroy a working profile, and must never
leave a broken one behind.

Fielded 7440d2d had both halves wrong: a failed `nmcli connection up` (wrong
password) left its autoconnect profile on disk so NM kept retrying the bad
password on its own and every reboot; and the pre-connect sweep deleted any
EXISTING profile for the same SSID (by substring, so "Shop" matched "Shop-5G")
BEFORE the attempt, so one typo knocked a working player offline and left
_restore_wifi_connection() pointing at a name that no longer existed.

Rules now under test (all in common.network._connect_wifi_secure):
  * failed activation  -> the NEW profile is discarded; pre-existing same-SSID
    profiles are untouched (restore always has a real name);
  * successful activation -> the new profile stays; older profiles for the
    same EXACT SSID are removed only now;
  * nmcli's client-side timeout is not a verdict: keep the profile while NM
    says it is still activating/active, discard only when NM says it is not,
    keep if NM cannot be asked.
Runs on a JAM Player (real modules); os.write / subprocess / nmcli are mocked
so NetworkManager is never touched.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402

UP = ['nmcli', 'connection', 'up']
DELETE = ['nmcli', 'connection', 'delete']


def _cp(rc=0, out='', err=''):
    return mock.MagicMock(returncode=rc, stdout=out, stderr=err)


class FakeNM:
    """A scriptable nmcli. `existing` maps saved profile name -> SSID."""

    def __init__(self, existing=None, up_rc=0, up_times_out=False,
                 state_after_timeout='', state_query_raises=False):
        self.existing = dict(existing or {})
        self.up_rc = up_rc
        self.up_times_out = up_times_out
        self.state_after_timeout = state_after_timeout
        self.state_query_raises = state_query_raises
        self.calls = []

    def run(self, cmd, *args, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:5] == ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection']:
            out = ''.join(f"{n}:802-11-wireless\n" for n in self.existing)
            return _cp(0, out)
        if cmd[:4] == ['nmcli', '-t', '-f', '802-11-wireless.ssid']:
            return _cp(0, f"802-11-wireless.ssid:{self.existing.get(cmd[-1], '')}\n")
        if cmd[:3] == UP:
            if self.up_times_out:
                raise subprocess.TimeoutExpired(cmd, 30)
            return _cp(self.up_rc, '', 'Secrets were required, but not provided' if self.up_rc else '')
        if cmd[:3] == ['nmcli', '-g', 'GENERAL.STATE']:
            if self.state_query_raises:
                raise OSError('dbus busy')
            return _cp(0, self.state_after_timeout + ('\n' if self.state_after_timeout else ''))
        if cmd[:3] == DELETE:
            self.existing.pop(cmd[3], None)
            return _cp(0)
        return _cp(0)  # reload etc.


class _Base(unittest.TestCase):

    def _attempt(self, nm: FakeNM, ssid='MyNet', password='pw'):
        with mock.patch.object(network.subprocess, "run", side_effect=nm.run), \
             mock.patch.object(network.os, "open", return_value=7), \
             mock.patch.object(network.os, "write", side_effect=lambda fd, d: len(d)), \
             mock.patch.object(network.os, "close"), \
             mock.patch.object(network.os, "chown"), \
             mock.patch.object(network.Path, "unlink"):
            return network._connect_wifi_secure(ssid, password)

    def _new_name(self, nm):
        ups = [c for c in nm.calls if c[:3] == UP]
        self.assertEqual(len(ups), 1, nm.calls)
        return ups[0][3]

    def _deletes_of(self, nm, name):
        return [c for c in nm.calls if c[:3] == DELETE and c[3] == name]

    def _index(self, nm, prefix, name=None):
        return next(i for i, c in enumerate(nm.calls)
                    if c[:3] == prefix and (name is None or c[3] == name))


class FailedProfileDiscardTests(_Base):

    def test_failed_activation_deletes_the_just_created_profile(self):
        nm = FakeNM(up_rc=4)
        result = self._attempt(nm)
        self.assertNotEqual(result.returncode, 0)
        name = self._new_name(nm)
        self.assertTrue(name.startswith('jam-wifi-'), name)
        self.assertEqual(len(self._deletes_of(nm, name)), 1,
                         'failed profile must be deleted so NM cannot keep retrying the bad password')

    def test_delete_happens_after_the_failed_up_not_before(self):
        nm = FakeNM(up_rc=4)
        self._attempt(nm)
        name = self._new_name(nm)
        self.assertGreater(self._index(nm, DELETE, name), self._index(nm, UP))

    def test_successful_activation_keeps_the_profile(self):
        nm = FakeNM(up_rc=0)
        result = self._attempt(nm)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self._deletes_of(nm, self._new_name(nm)), [])

    def test_cleanup_does_not_mask_the_failure(self):
        """connect_to_wifi maps this stderr to 'Invalid password'."""
        result = self._attempt(FakeNM(up_rc=4))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Secrets were required', result.stderr)

    def test_discard_never_raises_even_if_nmcli_is_gone(self):
        with mock.patch.object(network.subprocess, "run", side_effect=OSError("nmcli gone")), \
             mock.patch.object(network.Path, "exists", return_value=False):
            network._discard_failed_wifi_profile(
                'jam-wifi-deadbeef', Path('/nonexistent/jam-wifi-deadbeef.nmconnection'))

    def test_discard_falls_back_to_unlink_and_reload_when_nmcli_delete_leaves_the_file(self):
        calls = []
        def run(cmd, *a, **k):
            calls.append(list(cmd))
            return _cp(1, '', 'unknown connection')
        with mock.patch.object(network.subprocess, "run", side_effect=run), \
             mock.patch.object(network.Path, "exists", return_value=True), \
             mock.patch.object(network.Path, "unlink") as unlink:
            network._discard_failed_wifi_profile(
                'jam-wifi-cafe0001', Path('/x/jam-wifi-cafe0001.nmconnection'))
        unlink.assert_called_once()
        self.assertIn(['nmcli', 'connection', 'reload'], calls)


class ExistingProfileIsNeverDestroyedByAFailedAttemptTests(_Base):
    """The same-SSID sweep: collect before, delete only after success, exact match."""

    def test_existing_good_profile_for_same_ssid_survives_a_failed_attempt(self):
        nm = FakeNM(existing={'jam-wifi-old': 'MyNet'}, up_rc=4)
        self._attempt(nm, ssid='MyNet', password='typo')
        self.assertEqual(self._deletes_of(nm, 'jam-wifi-old'), [],
                         'a typo must not delete the working profile')
        self.assertIn('jam-wifi-old', nm.existing)
        self.assertEqual(len(self._deletes_of(nm, self._new_name(nm))), 1)

    def test_existing_same_ssid_profile_is_replaced_only_after_success(self):
        nm = FakeNM(existing={'jam-wifi-old': 'MyNet'}, up_rc=0)
        result = self._attempt(nm, ssid='MyNet')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self._deletes_of(nm, 'jam-wifi-old')), 1, 'superseded profile removed')
        self.assertGreater(self._index(nm, DELETE, 'jam-wifi-old'), self._index(nm, UP),
                           'removal must come AFTER the new profile activated')
        self.assertEqual(self._deletes_of(nm, self._new_name(nm)), [])

    def test_ssid_match_is_exact_not_substring(self):
        """'Shop' must not touch the profile for 'Shop-5G' (the old `in` test did)."""
        nm = FakeNM(existing={'jam-wifi-5g': 'Shop-5G', 'jam-wifi-guest': 'Shop-Guest'}, up_rc=0)
        self._attempt(nm, ssid='Shop')
        self.assertEqual(self._deletes_of(nm, 'jam-wifi-5g'), [])
        self.assertEqual(self._deletes_of(nm, 'jam-wifi-guest'), [])
        self.assertEqual(set(nm.existing), {'jam-wifi-5g', 'jam-wifi-guest'})

    def test_find_profiles_unescapes_nmcli_colons_and_matches_exactly(self):
        nm = FakeNM(existing={'cafe': 'Cafe: Main', 'other': 'Cafe'})
        # nmcli -t escapes ':' as '\:' in values; the fake echoes raw, so escape here.
        nm.existing['cafe'] = 'Cafe\\: Main'
        with mock.patch.object(network.subprocess, "run", side_effect=nm.run):
            self.assertEqual(network._find_wifi_profiles_for_ssid('Cafe: Main'), ['cafe'])
            self.assertEqual(network._find_wifi_profiles_for_ssid('Cafe'), ['other'])


class ClientTimeoutIsNotAVerdictTests(_Base):
    """nmcli's 30 s client timeout vs NetworkManager's ~90 s activation."""

    def test_timeout_keeps_profile_when_nm_is_still_activating(self):
        nm = FakeNM(up_times_out=True, state_after_timeout='activating')
        result = self._attempt(nm)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('timed out', result.stderr)   # BLE maps this to 'timeout'
        self.assertEqual(self._deletes_of(nm, self._new_name(nm)), [],
                         'a still-activating profile must not be destroyed')

    def test_timeout_keeps_profile_when_already_activated(self):
        nm = FakeNM(up_times_out=True, state_after_timeout='activated')
        self._attempt(nm)
        self.assertEqual(self._deletes_of(nm, self._new_name(nm)), [])

    def test_timeout_discards_profile_when_nm_says_it_is_not_activating(self):
        nm = FakeNM(up_times_out=True, state_after_timeout='')   # inactive: NM gave up
        self._attempt(nm)
        self.assertEqual(len(self._deletes_of(nm, self._new_name(nm))), 1)

    def test_timeout_keeps_profile_when_nm_cannot_be_asked(self):
        nm = FakeNM(up_times_out=True, state_query_raises=True)
        self._attempt(nm)
        self.assertEqual(self._deletes_of(nm, self._new_name(nm)), [], 'fail-safe is KEEP')


class PriorityPromotionActuallyRunsTests(unittest.TestCase):
    """69b46b6 left `threading` unbound in network.py, so _promote_in_background
    raised NameError on every successful connect and promotion never happened."""

    def test_promote_in_background_starts_a_thread(self):
        target = mock.MagicMock()
        with mock.patch.object(network.threading, "Thread") as thread_cls:
            network._promote_in_background(target)
        thread_cls.assert_called_once()
        self.assertIs(thread_cls.call_args.kwargs.get('target'), target)
        thread_cls.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
