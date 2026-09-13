"""
jam-update must never tear down a live BLE setup session.

The first-connect auto-update starts seconds after the customer joins WiFi
from the app, and its unconditional `systemctl restart bluetooth` (the BLE unit
is PartOf= it) plus the BLE unit restarts landed right as they were registering
-- a BLE write 30 s-3 min later -- dropping the link so the device looked
broken. Now: BLE configs are installed only when their bytes differ (and
atomically), bluetoothd is restarted only when main.conf changed, and both wait
for any session to end (bounded) before touching BLE. Runs on a JAM Player
(jam_update imports the device venv); systemctl/busctl are mocked.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_update  # noqa: E402


def _busctl_json(connected):
    return json.dumps({"type": "a{oa{sa{sv}}}", "data": [{
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": {"type": "b", "data": True}}},
        "/org/bluez/hci0/dev_AA_BB": {"org.bluez.Device1": {"Connected": {"type": "b", "data": connected}}},
    }]})


class SessionDetectionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flag = Path(self.tmp.name) / 'ble_session_active'
        self._p = mock.patch.object(jam_update, 'BLE_SESSION_ACTIVE_FLAG', self.flag); self._p.start()

    def tearDown(self):
        self._p.stop(); self.tmp.cleanup()

    def test_fresh_connect_flag_means_active_without_asking_bluez(self):
        self.flag.touch()
        with mock.patch.object(jam_update, 'run_command', side_effect=AssertionError('must not call busctl')):
            self.assertTrue(jam_update._ble_session_active())

    def test_stale_flag_is_ignored_and_bluez_is_asked(self):
        self.flag.touch()
        old = time.time() - jam_update._BLE_SESSION_FLAG_MAX_AGE_SEC - 60
        os.utime(self.flag, (old, old))
        with mock.patch.object(jam_update, 'run_command', return_value=(True, _busctl_json(False), '')):
            self.assertFalse(jam_update._ble_session_active())

    def test_connected_phone_means_active(self):
        with mock.patch.object(jam_update, 'run_command', return_value=(True, _busctl_json(True), '')):
            self.assertTrue(jam_update._ble_session_active())

    def test_busctl_failure_means_not_active(self):
        """No D-Bus -> no session to protect; a busctl problem must not stall updates."""
        with mock.patch.object(jam_update, 'run_command', return_value=(False, '', 'no bus')):
            self.assertFalse(jam_update._ble_session_active())
        with mock.patch.object(jam_update, 'run_command', return_value=(True, 'not json', '')):
            self.assertFalse(jam_update._ble_session_active())


class WaitForSessionTests(unittest.TestCase):

    def test_no_session_returns_immediately(self):
        with mock.patch.object(jam_update, '_ble_session_active', return_value=False), \
             mock.patch.object(jam_update.time, 'sleep') as slept:
            self.assertTrue(jam_update._wait_for_ble_session_to_end())
        slept.assert_not_called()

    def test_session_that_ends_is_waited_for(self):
        states = iter([True, True, False])
        with mock.patch.object(jam_update, '_ble_session_active', side_effect=lambda: next(states)), \
             mock.patch.object(jam_update.time, 'sleep'):
            self.assertTrue(jam_update._wait_for_ble_session_to_end(max_wait=60))

    def test_session_that_never_ends_gives_up_after_budget(self):
        with mock.patch.object(jam_update, '_ble_session_active', return_value=True), \
             mock.patch.object(jam_update.time, 'sleep'):
            self.assertFalse(jam_update._wait_for_ble_session_to_end(max_wait=20))


class InstallOnlyWhenChangedTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src_root = root / 'etc'
        (self.src_root / 'bluetooth').mkdir(parents=True)
        (self.src_root / 'dbus-1' / 'system.d').mkdir(parents=True)
        (self.src_root / 'bluetooth' / 'main.conf').write_text('[General]\nJustWorksRepairing = always\n')
        (self.src_root / 'dbus-1' / 'system.d' / 'jam-ble-provisioning.conf').write_text('<busconfig/>\n')
        self.bt_dest = root / 'dest' / 'main.conf'
        self.dbus_dest = root / 'dest' / 'jam-ble-provisioning.conf'
        self.patches = [
            mock.patch.object(jam_update, 'ETC_SRC', self.src_root),
            mock.patch.object(jam_update, '_BLUEZ_MAIN_CONF_DEST', self.bt_dest),
            mock.patch.object(jam_update, '_DBUS_POLICY_DEST', self.dbus_dest),
            mock.patch.object(jam_update.os, 'chown'),
        ]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def test_first_install_writes_and_reports_change(self):
        self.assertTrue(jam_update.install_ble_configs())
        self.assertEqual(self.bt_dest.read_text(), '[General]\nJustWorksRepairing = always\n')
        self.assertTrue(self.dbus_dest.exists())

    def test_identical_bytes_do_not_rewrite_and_report_no_change(self):
        jam_update.install_ble_configs()
        before = self.bt_dest.stat().st_mtime_ns
        self.assertFalse(jam_update.install_ble_configs(), 'unchanged main.conf must not trigger a bluetooth restart')
        self.assertEqual(self.bt_dest.stat().st_mtime_ns, before, 'unchanged file must not be rewritten')

    def test_install_never_restarts_bluetooth_itself(self):
        with mock.patch.object(jam_update, 'run_command', side_effect=AssertionError('no systemctl here')):
            jam_update.install_ble_configs()


class RestartGuardTests(unittest.TestCase):

    def _restart(self, session_clears, bluetooth_changed):
        calls = []
        def run(cmd, *a, **k):
            calls.append(list(cmd)); return (True, 'active', '')
        with mock.patch.object(jam_update, '_wait_for_ble_session_to_end', return_value=session_clears), \
             mock.patch.object(jam_update, 'run_command', side_effect=run), \
             mock.patch.object(jam_update.time, 'sleep'):
            jam_update.restart_services(bluetooth_conf_changed=bluetooth_changed)
        return calls

    def _restarted(self, calls):
        return [c[2] for c in calls if c[:2] == ['systemctl', 'restart']]

    def test_active_session_skips_ble_units_and_bluetoothd(self):
        restarted = self._restarted(self._restart(session_clears=False, bluetooth_changed=True))
        self.assertNotIn('bluetooth', restarted)
        self.assertNotIn('jam-ble-provisioning.service', restarted)
        self.assertNotIn('jam-ble-state-manager.service', restarted)
        self.assertTrue(any(s.startswith('jam-') for s in restarted), 'other services still restart')

    def test_clear_session_with_changed_conf_restarts_bluetoothd_once(self):
        restarted = self._restarted(self._restart(session_clears=True, bluetooth_changed=True))
        self.assertEqual(restarted.count('bluetooth'), 1)
        self.assertIn('jam-ble-provisioning.service', restarted)

    def test_unchanged_conf_never_restarts_bluetoothd(self):
        restarted = self._restarted(self._restart(session_clears=True, bluetooth_changed=False))
        self.assertNotIn('bluetooth', restarted)


if __name__ == '__main__':
    unittest.main()
