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
    m._session_hold_logged = False
    # Every other flag __init__ would have set, so real methods can run on this bare instance.
    m._pending_action = None
    m._internet_check_timer = None
    m._first_connect_update_triggered = False
    # No setup session unless a test says otherwise (the real check talks to
    # BlueZ and reads a tmpfs flag; neither is meaningful in a unit test).
    m._setup_session_in_progress = lambda: False
    return m


WINDOW = sm.BLE_BOOT_RECOVERY_WINDOW_SECONDS


class RecoveryWindowTests(unittest.TestCase):
    def test_window_is_fifteen_minutes(self):
        self.assertEqual(WINDOW, 15 * 60)

    def _should_run(self, uptime, is_online, registered, method='jam_backend'):
        with mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None), \
             mock.patch.object(sm, 'is_device_registered', return_value=registered):
            return _manager()._should_ble_run(is_online, method)

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

    # --- the window counts from when BLE actually came up -------------------
    def _run_with_activation(self, uptime, ble_active_since):
        with mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=ble_active_since), \
             mock.patch.object(sm, 'is_device_registered', return_value=True):
            return _manager()._should_ble_run(True, 'jam_backend')

    def test_a_delayed_ble_start_gets_its_full_window(self):
        """BLE came up 20 min after power-on (slow boot, repair, restart):
        the guarantee counts from then, not from power-on."""
        self.assertTrue(self._run_with_activation(uptime=1300, ble_active_since=1200))
        self.assertTrue(self._run_with_activation(uptime=2099, ble_active_since=1200))
        self.assertFalse(self._run_with_activation(uptime=2100, ble_active_since=1200))

    def test_unknown_activation_time_falls_back_to_the_power_on_rule(self):
        """systemd unavailable / unit never active: unknown must not extend
        OR shrink the window -- the power-on rule stands alone."""
        self.assertFalse(self._run_with_activation(uptime=WINDOW + 1, ble_active_since=None))
        with mock.patch.object(sm, 'seconds_since_boot', return_value=10.0), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None):
            self.assertTrue(_manager()._should_ble_run(True, 'jam_backend'))

    def test_systemd_is_not_asked_while_the_power_on_window_is_still_open(self):
        """No subprocess per tick for the first 15 minutes."""
        with mock.patch.object(sm, 'seconds_since_boot', return_value=60.0), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', side_effect=AssertionError('must not query')):
            self.assertTrue(_manager()._in_boot_recovery_window())

    # --- after the window: the pre-existing rule ---------------------------
    def test_after_window_online_registered_stops_ble(self):
        self.assertFalse(self._should_run(WINDOW, is_online=True, registered=True))


class SetupSessionHoldTests(unittest.TestCase):
    """The window must never close on somebody who is mid-setup."""

    def _manager_with_session(self, in_progress):
        m = _manager()
        m._setup_session_in_progress = lambda: in_progress
        return m

    def _run(self, m, uptime):
        with mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None), \
             mock.patch.object(sm, 'is_device_registered', return_value=True):
            return m._should_ble_run(True, 'jam_backend')

    def test_an_active_session_holds_the_window_open_past_its_end(self):
        self.assertTrue(self._run(self._manager_with_session(True), WINDOW + 60))

    def test_no_session_lets_the_window_close(self):
        self.assertFalse(self._run(self._manager_with_session(False), WINDOW + 60))

    def test_the_hold_is_logged_once_not_every_tick(self):
        m = self._manager_with_session(True)
        self._run(m, WINDOW + 60)
        self.assertTrue(m._session_hold_logged)
        self._run(m, WINDOW + 67)
        self.assertTrue(m._session_hold_logged)

    def _flag(self, age_seconds):
        flag = mock.MagicMock()
        flag.exists.return_value = True
        flag.stat.return_value = mock.MagicMock(st_mtime=1_000_000.0)
        return flag, 1_000_000.0 + age_seconds

    def test_a_wifi_connect_in_flight_counts_as_a_session(self):
        m = _manager()
        del m._setup_session_in_progress  # use the real implementation
        flag, now = self._flag(age_seconds=10)
        with mock.patch.object(sm, 'BLE_SESSION_ACTIVE_FLAG', flag), \
             mock.patch.object(sm.time, 'time', return_value=now):
            self.assertTrue(m._setup_session_in_progress())

    def test_a_session_hours_into_the_boot_is_still_honoured(self):
        """No overall deadline: availability beats tidiness. A tech starting
        setup two hours after boot must not be cut off."""
        m = _manager()
        del m._setup_session_in_progress
        flag, now = self._flag(age_seconds=5)
        with mock.patch.object(sm, 'BLE_SESSION_ACTIVE_FLAG', flag), \
             mock.patch.object(sm.time, 'time', return_value=now), \
             mock.patch.object(sm, 'seconds_since_boot', return_value=7200):
            self.assertTrue(m._setup_session_in_progress())

    def test_a_marker_nobody_cleared_goes_stale_instead_of_holding_forever(self):
        m = _manager()
        del m._setup_session_in_progress
        flag, now = self._flag(age_seconds=sm.BLE_SESSION_FLAG_MAX_AGE_SECONDS + 1)
        m.bus = mock.MagicMock()
        m.bus.get_object.side_effect = Exception('no bluez')
        with mock.patch.object(sm, 'BLE_SESSION_ACTIVE_FLAG', flag), \
             mock.patch.object(sm.time, 'time', return_value=now):
            self.assertFalse(m._setup_session_in_progress())

    def test_a_dbus_failure_does_not_hold_ble_open(self):
        m = _manager()
        del m._setup_session_in_progress
        flag = mock.MagicMock()
        flag.exists.return_value = False
        m.bus = mock.MagicMock()
        m.bus.get_object.side_effect = Exception('no bluez')
        with mock.patch.object(sm, 'BLE_SESSION_ACTIVE_FLAG', flag):
            self.assertFalse(m._setup_session_in_progress())

    def test_after_window_offline_keeps_ble_up(self):
        self.assertTrue(self._should_run(WINDOW + 3600, is_online=False, registered=True))

    def test_after_window_unregistered_keeps_ble_up(self):
        self.assertTrue(self._should_run(WINDOW + 3600, is_online=True, registered=False))

    def test_after_window_online_only_via_fallback_keeps_ble_up(self):
        """A guest network that firewalls our API but passes 443 to Cloudflare
        is 'online' by the fallback probe and unreachable for support: degraded."""
        for method in ('cloudflare_tls', 'google_tls', 'unknown', 'none'):
            self.assertTrue(self._should_run(WINDOW + 60, is_online=True, registered=True, method=method), method)

    def test_only_a_reachable_backend_can_stop_ble(self):
        self.assertFalse(self._should_run(WINDOW + 60, is_online=True, registered=True, method='jam_backend'))


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
        m._connectivity_monitor.last_success_method.return_value = 'jam_backend'
        m._get_current_state = mock.MagicMock(return_value=70)  # NM_STATE_CONNECTED_GLOBAL
        with mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None), \
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
        # ...and it is actively RE-ASSERTED every tick, so a crashed BLE comes back.
        manage.assert_called_with(sm.BLE_PROVISIONING_SERVICE, should_run=True)

    def test_nm_disconnected_tick_reasserts_ble(self):
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._get_current_state = mock.MagicMock(return_value=20)  # NM_STATE_DISCONNECTED
        with mock.patch.object(sm, 'manage_service') as manage:
            self.assertTrue(m._periodic_connectivity_check())
        manage.assert_called_once_with(sm.BLE_PROVISIONING_SERVICE, should_run=True)
        m._connectivity_monitor.check.assert_not_called()

    def test_periodic_check_survives_an_exception(self):
        """PyGObject drops a raising timeout source; the loop must never raise."""
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._get_current_state = mock.MagicMock(side_effect=OSError('EIO'))
        with mock.patch.object(sm, 'manage_service'):
            self.assertTrue(m._periodic_connectivity_check())

    def test_boot_that_comes_up_online_reruns_the_gated_oneshots(self):
        """tailscale/announce/installed-version exit 0 when they start before
        the verified flag exists; a boot that then finds the device online
        must re-run them, or they sit 'active (exited)' until the nightly
        reboot."""
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._get_current_state = mock.MagicMock(return_value=70)  # NM connected
        m._last_connected_state = None
        m._restart_post_connectivity_services = mock.MagicMock()
        with mock.patch.object(sm, 'INTERNET_VERIFIED_FLAG'), \
             mock.patch.object(sm, 'check_internet_connectivity', return_value=(True, 'jam_backend')), \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'manage_service'), \
             mock.patch.object(sm, 'safe_touch'), \
             mock.patch.object(sm, 'seconds_since_boot', return_value=5.0), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None):
            m.check_initial_state()
        m._restart_post_connectivity_services.assert_called_once()

    def test_boot_that_comes_up_online_on_a_retry_also_reruns_them(self):
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._get_current_state = mock.MagicMock(return_value=70)
        m._last_connected_state = None
        m._restart_post_connectivity_services = mock.MagicMock()
        results = iter([(False, 'none'), (True, 'jam_backend')])
        with mock.patch.object(sm, 'INTERNET_VERIFIED_FLAG'), \
             mock.patch.object(sm, 'check_internet_connectivity', side_effect=lambda *a, **k: next(results)), \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'manage_service'), \
             mock.patch.object(sm, 'safe_touch'), \
             mock.patch.object(sm, 'seconds_since_boot', return_value=5.0), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None), \
             mock.patch.object(sm.time, 'sleep'):
            m.check_initial_state()
        m._restart_post_connectivity_services.assert_called_once()

    def test_boot_that_stays_offline_does_not_rerun_them(self):
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._get_current_state = mock.MagicMock(return_value=20)  # NM disconnected
        m._last_connected_state = None
        m._restart_post_connectivity_services = mock.MagicMock()
        with mock.patch.object(sm, 'INTERNET_VERIFIED_FLAG'), \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'manage_service'), \
             mock.patch.object(sm, 'seconds_since_boot', return_value=5.0):
            m.check_initial_state()
        m._restart_post_connectivity_services.assert_not_called()

    def test_initial_state_clears_the_stale_online_flag_first(self):
        """A power-cycle onto a captive portal must not inherit last boot's 'online'."""
        m = _manager()
        m._connectivity_monitor = mock.MagicMock()
        m._get_current_state = mock.MagicMock(return_value=20)
        m._last_connected_state = None
        with mock.patch.object(sm, 'INTERNET_VERIFIED_FLAG') as flag, \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'manage_service'), \
             mock.patch.object(sm, 'seconds_since_boot', return_value=5.0):
            m.check_initial_state()
        flag.unlink.assert_any_call(missing_ok=True)

    def test_after_window_periodic_check_stops_ble_and_logs_once(self):
        manage, m = self._run_periodic(uptime=WINDOW + 7)
        manage.assert_called_with(sm.BLE_PROVISIONING_SERVICE, should_run=False)
        self.assertTrue(m._recovery_window_closed_logged)
        # Second tick: still stops (idempotent), but the log gate stays set.
        with mock.patch.object(sm, 'seconds_since_boot', return_value=WINDOW + 14), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None), \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'manage_service') as manage2:
            m._periodic_connectivity_check()
        manage2.assert_called_with(sm.BLE_PROVISIONING_SERVICE, should_run=False)


if __name__ == '__main__':
    unittest.main()
