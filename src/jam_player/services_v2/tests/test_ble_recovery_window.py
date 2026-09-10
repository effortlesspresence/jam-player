"""
The post-boot BLE recovery window.

BLE provisioning must run for BLE_BOOT_RECOVERY_WINDOW_SECONDS after every
boot no matter what the device believes about connectivity or registration.
This is the guarantee that a player stranded on a captive-portal / firewalled
network can always be rescued by a power-cycle + the mobile app.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_ble_state_manager as sm  # noqa: E402


def _manager():
    """A BLEStateManager without touching D-Bus: bypass __init__, set fields."""
    m = object.__new__(sm.BLEStateManager)
    m._recovery_window_closed_logged = False
    return m


WINDOW = sm.BLE_BOOT_RECOVERY_WINDOW_SECONDS


class RecoveryWindowTests(unittest.TestCase):
    def test_window_is_fifteen_minutes(self):
        self.assertEqual(WINDOW, 15 * 60)

    def _should_run(self, uptime, is_online, registered):
        with mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'is_device_registered', return_value=registered):
            return _manager()._should_ble_run(is_online)

    # --- inside the window: ALWAYS on -------------------------------------
    def test_just_booted_online_registered_keeps_ble_up(self):
        """The case that used to strand devices: 'online' + registered at boot."""
        self.assertTrue(self._should_run(0.0, is_online=True, registered=True))

    def test_last_second_of_window_online_registered_keeps_ble_up(self):
        self.assertTrue(self._should_run(WINDOW - 1, is_online=True, registered=True))

    def test_window_does_not_consult_registration(self):
        """Inside the window the answer is True before registration is even read."""
        with mock.patch.object(sm, 'seconds_since_boot', return_value=10.0), \
             mock.patch.object(sm, 'is_device_registered', side_effect=AssertionError('must not be called')):
            self.assertTrue(_manager()._should_ble_run(True))

    def test_unreadable_uptime_keeps_window_open(self):
        """seconds_since_boot fails SAFE to 0.0 -> window open -> BLE up."""
        self.assertTrue(self._should_run(0.0, is_online=True, registered=True))

    # --- after the window: the pre-existing rule ---------------------------
    def test_after_window_online_registered_stops_ble(self):
        self.assertFalse(self._should_run(WINDOW, is_online=True, registered=True))

    def test_after_window_offline_keeps_ble_up(self):
        self.assertTrue(self._should_run(WINDOW + 3600, is_online=False, registered=True))

    def test_after_window_unregistered_keeps_ble_up(self):
        self.assertTrue(self._should_run(WINDOW + 3600, is_online=True, registered=False))


class PeriodicCheckClosesWindowTests(unittest.TestCase):
    """
    The 7-second periodic check is what actually stops BLE when the window
    closes on an online+registered device (no connectivity transition ever
    happens in that case, so the transition path can't be relied on).
    """

    def _run_periodic(self, uptime, registered=True):
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._connectivity_monitor.check.return_value = True
        m._connectivity_monitor.state_changed = False
        m._get_current_state = mock.MagicMock(return_value=70)  # NM_STATE_CONNECTED_GLOBAL
        with mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'is_device_registered', return_value=registered), \
             mock.patch.object(sm, 'manage_service') as manage:
            keep_going = m._periodic_connectivity_check()
        self.assertTrue(keep_going)
        return manage, m

    def test_inside_window_periodic_check_never_stops_ble(self):
        manage, _ = self._run_periodic(uptime=60)
        self.assertFalse(
            any(c.kwargs.get('should_run') is False for c in manage.call_args_list),
            'BLE must not be stopped while the recovery window is open',
        )

    def test_after_window_periodic_check_stops_ble_and_logs_once(self):
        manage, m = self._run_periodic(uptime=WINDOW + 7)
        manage.assert_called_with(sm.BLE_PROVISIONING_SERVICE, should_run=False)
        self.assertTrue(m._recovery_window_closed_logged)
        # Second tick: still stops (idempotent), but the log gate stays set.
        with mock.patch.object(sm, 'seconds_since_boot', return_value=WINDOW + 14), \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'manage_service') as manage2:
            m._periodic_connectivity_check()
        manage2.assert_called_with(sm.BLE_PROVISIONING_SERVICE, should_run=False)


if __name__ == '__main__':
    unittest.main()
