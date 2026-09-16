"""
jam-update must never tear down a live BLE setup session -- and must never
make the rest of the player wait for one.

The first-connect auto-update starts seconds after the customer joins WiFi
from the app, and its unconditional `systemctl restart bluetooth` (the BLE unit
is PartOf= it) plus the BLE unit restarts landed right as they were registering
-- a BLE write 30 s-3 min later -- dropping the link so the device looked
broken. Now: BLE configs are installed only when their bytes differ (and
atomically); every non-Bluetooth service is restarted first, immediately; then
bluetoothd (only when main.conf changed) and the two BLE units are restarted
only if no session is live at that moment, otherwise left alone until the next
boot. There is deliberately no waiting: the earlier five-minute wait held every
other restart, outlived jam-player-display's stale-flag budget, and (jam-update
being Type=oneshot) would have pinned any unit ordered After= it.

Runs on a JAM Player (jam_update imports the device venv); systemctl/busctl
are mocked.
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
    NON_BLE = ('jam-player-display.service', 'jam-heartbeat.service', 'jam-tailscale.service',
               'jam-content-manager.service')
    BLE = ('jam-ble-provisioning.service', 'jam-ble-state-manager.service')
    SESSION_CHECK = '<ble-session-check>'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.marker = root / 'run' / 'jam' / 'first_connect_update_triggered'
        self.update_flag = root / 'run' / 'jam-update-in-progress'
        for name, value in (('FIRST_CONNECT_UPDATE_FLAG', self.marker),
                            ('UPDATE_IN_PROGRESS_FLAG', self.update_flag)):
            patcher = mock.patch.object(jam_update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restart(self, session_active, bluetooth_changed):
        """Run restart_services; return every systemctl call in order, with a
        marker where the BLE session was consulted."""
        calls = []
        def run(cmd, *a, **k):
            calls.append(list(cmd)); return (True, 'active', '')
        def session():
            calls.append([self.SESSION_CHECK]); return session_active
        with mock.patch.object(jam_update, '_ble_session_active', side_effect=session), \
             mock.patch.object(jam_update, 'run_command', side_effect=run), \
             mock.patch.object(jam_update.time, 'sleep') as slept:
            jam_update.restart_services(bluetooth_conf_changed=bluetooth_changed)
        self.slept = slept
        return calls

    @staticmethod
    def _restarted(calls):
        return [c[-1] for c in calls if c[:2] == ['systemctl', 'restart']]

    def test_active_session_skips_ble_units_and_bluetoothd(self):
        restarted = self._restarted(self._restart(session_active=True, bluetooth_changed=True))
        self.assertNotIn('bluetooth', restarted)
        for unit in self.BLE:
            self.assertNotIn(unit, restarted)
        for unit in self.NON_BLE:
            self.assertIn(unit, restarted, 'every other service still restarts')

    def test_clear_session_with_changed_conf_restarts_bluetoothd_once(self):
        restarted = self._restarted(self._restart(session_active=False, bluetooth_changed=True))
        self.assertEqual(restarted.count('bluetooth'), 1)
        for unit in self.BLE:
            self.assertIn(unit, restarted)

    def test_unchanged_conf_never_restarts_bluetoothd(self):
        restarted = self._restarted(self._restart(session_active=False, bluetooth_changed=False))
        self.assertNotIn('bluetooth', restarted)
        for unit in self.BLE:
            self.assertIn(unit, restarted)

    def test_every_other_service_restarts_before_the_session_is_consulted(self):
        """The bench fault: heartbeat, tailscale and the display sat on old code
        for five minutes because the BLE decision came first."""
        calls = self._restart(session_active=False, bluetooth_changed=True)
        check_at = calls.index([self.SESSION_CHECK])
        for i, cmd in enumerate(calls):
            if cmd[:2] != ['systemctl', 'restart']:
                continue
            unit = cmd[-1]
            if unit in self.BLE or unit == 'bluetooth':
                self.assertGreater(i, check_at, f'{unit} must be decided by the session check')
            else:
                self.assertLess(i, check_at, f'{unit} must restart before the BLE decision')

    def test_a_live_session_is_decided_once_and_never_waited_for(self):
        calls = self._restart(session_active=True, bluetooth_changed=True)
        self.assertEqual(calls.count([self.SESSION_CHECK]), 1, 'no polling')
        # The only sleep left is the 5 s settle before verifying service status.
        self.assertTrue(all(c.args[0] <= 5 for c in self.slept.call_args_list),
                        f'unexpected wait: {self.slept.call_args_list}')

    def test_first_connect_marker_is_set_whether_or_not_ble_units_restart(self):
        self._restart(session_active=True, bluetooth_changed=False)
        self.assertTrue(self.marker.exists(), 'marker set even when the state manager is left alone')
        self.marker.unlink()
        self._restart(session_active=False, bluetooth_changed=False)
        self.assertTrue(self.marker.exists(), 'marker set before the state manager restarts onto new code')

    def test_updating_flag_is_refreshed_before_the_display_restarts(self):
        """A long install must not leave the freshly restarted display thinking
        the updater's screen is stale."""
        self.update_flag.parent.mkdir(parents=True)
        self.update_flag.touch()
        stale = time.time() - 15 * 60
        os.utime(self.update_flag, (stale, stale))
        self._restart(session_active=False, bluetooth_changed=False)
        self.assertGreater(self.update_flag.stat().st_mtime, stale + 60, 'flag mtime not refreshed')

    def test_no_updating_flag_is_not_created_by_the_refresh(self):
        self._restart(session_active=False, bluetooth_changed=False)
        self.assertFalse(self.update_flag.exists(), 'refresh must never create the flag')

if __name__ == '__main__':
    unittest.main()
