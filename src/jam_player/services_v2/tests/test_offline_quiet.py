"""
Offline services must not write to the SD card in a loop.

A JAM Player that cannot reach the backend used to produce hundreds of
WARNING/ERROR journal entries per hour from per-minute timers, restart
loops and reconnect loops. Every one of those is a write to an SD card that
we already replace for wear. The rule: keep the FIRST occurrence (that is
the signal), ship the repeats, and never fail in a way that makes systemd
restart the unit while it is simply offline.

On-device only (real modules). Nothing here touches the network.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


class DeviceIsOfflineTests(unittest.TestCase):
    """The flag is a cache; these tests pin BOTH ways it can be unknown."""

    def _fresh(self):
        return mock.patch.object(network, 'connectivity_state_is_fresh', return_value=True)

    def test_is_internet_verified_fails_in_the_direction_the_caller_chooses(self):
        flag = mock.MagicMock()
        flag.exists.side_effect = OSError('EIO')
        with self._fresh(), mock.patch.object(network, 'INTERNET_VERIFIED_FLAG', flag):
            self.assertFalse(network.is_internet_verified())                       # display / BLE: unknown = offline
            self.assertTrue(network.is_internet_verified(unreadable_means=True))    # oneshot gates: unknown = online

    def test_true_when_the_verified_flag_is_absent(self):
        flag = mock.MagicMock()
        flag.exists.return_value = False
        with self._fresh(), mock.patch.object(network, 'INTERNET_VERIFIED_FLAG', flag):
            self.assertTrue(network.device_is_offline())

    def test_false_when_the_flag_is_present(self):
        flag = mock.MagicMock()
        flag.exists.return_value = True
        with self._fresh(), mock.patch.object(network, 'INTERNET_VERIFIED_FLAG', flag):
            self.assertFalse(network.device_is_offline())

    def test_fails_open_when_the_flag_cannot_be_read(self):
        """A broken flag must never silently disable a service."""
        flag = mock.MagicMock()
        flag.exists.side_effect = OSError('EIO')
        with self._fresh(), mock.patch.object(network, 'INTERNET_VERIFIED_FLAG', flag):
            self.assertFalse(network.device_is_offline())

    def test_a_dead_state_manager_makes_the_answer_unknown_not_stale(self):
        """The flag says 'absent' but nobody has maintained it for a while:
        the gates must run (fail open) and the display/BLE must say offline."""
        flag = mock.MagicMock()
        flag.exists.return_value = False
        with mock.patch.object(network, 'connectivity_state_is_fresh', return_value=False), \
             mock.patch.object(network, 'INTERNET_VERIFIED_FLAG', flag):
            self.assertFalse(network.device_is_offline(), 'a frozen flag must not disable a service')
            self.assertFalse(network.is_internet_verified())
            flag.exists.assert_not_called()  # the flag is not even consulted when unknown

    def test_freshness_is_the_stamp_age(self):
        stamp = mock.MagicMock()
        stamp.stat.return_value = mock.MagicMock(st_mtime=1_000_000.0)
        with mock.patch.object(network, 'STATE_MANAGER_ALIVE_FLAG', stamp):
            with mock.patch.object(network.time, 'time', return_value=1_000_030.0):
                self.assertTrue(network.connectivity_state_is_fresh())
            with mock.patch.object(network.time, 'time', return_value=1_000_000.0 + network.CONNECTIVITY_STATE_MAX_AGE_SECONDS + 1):
                self.assertFalse(network.connectivity_state_is_fresh())

    def test_never_stamped_this_boot_is_not_fresh(self):
        stamp = mock.MagicMock()
        stamp.stat.side_effect = FileNotFoundError()
        with mock.patch.object(network, 'STATE_MANAGER_ALIVE_FLAG', stamp):
            self.assertFalse(network.connectivity_state_is_fresh())


class OneshotOfflineGateTests(unittest.TestCase):
    """The oneshots exit 0 (no restart loop) and log nothing above DEBUG."""

    def test_installed_version_reporter_exits_zero_and_stays_quiet(self):
        import jam_installed_version_reporter as mod
        with mock.patch.object(mod, 'device_is_offline', return_value=True), \
             mock.patch.object(mod, 'read_installed_version', return_value='2.0.1'), \
             mock.patch.object(mod, 'report_installed_version_to_backend') as report, \
             self.assertLogs(level='DEBUG') as logs:
            self.assertEqual(mod.main(), 0, 'must exit 0 so systemd does not restart it')
        report.assert_not_called()
        self.assertEqual([r for r in logs.records if r.levelno >= 30], [],
                         'nothing above INFO may reach the card while merely offline')

    def test_registration_poller_exits_zero_and_never_calls_the_api(self):
        import jam_registration_poller as mod
        with mock.patch.object(mod, 'device_is_offline', return_value=True), \
             mock.patch.object(mod, 'is_device_registered', return_value=False), \
             mock.patch.object(mod, 'check_registration_status') as check, \
             self.assertLogs(level='DEBUG') as logs:
            with self.assertRaises(SystemExit) as exit_ctx:
                mod.main()
        self.assertEqual(exit_ctx.exception.code, 0)
        check.assert_not_called()
        self.assertEqual([r for r in logs.records if r.levelno >= 30], [])

    def test_announce_exits_zero_without_gathering_credentials(self):
        import jam_announce as mod
        with mock.patch.object(mod, 'device_is_offline', return_value=True), \
             mock.patch.object(mod, 'is_device_announced', return_value=False), \
             mock.patch.object(mod, 'get_device_uuid') as uuid, \
             self.assertLogs(level='DEBUG') as logs:
            with self.assertRaises(SystemExit) as exit_ctx:
                mod.main()
        self.assertEqual(exit_ctx.exception.code, 0)
        uuid.assert_not_called()
        self.assertEqual([r for r in logs.records if r.levelno >= 30], [])


class LoopDedupTests(unittest.TestCase):
    def test_websocket_errors_keep_only_the_first_then_report_recovery(self):
        import jam_ws_commands as mod
        mod._ws_consecutive_errors = 0
        with self.assertLogs(level='DEBUG') as logs:
            for _ in range(5):
                mod.on_error(None, 'connection refused')
        levels = [r.levelname for r in logs.records if 'connection refused' in r.getMessage()]
        self.assertEqual(levels, ['WARNING', 'DEBUG', 'DEBUG', 'DEBUG', 'DEBUG'])

        with self.assertLogs(level='DEBUG') as recovered:
            try:
                mod.on_open(None)
            except Exception:
                pass  # on_open does more than reset the counter; only the reset matters here
        self.assertTrue(any('reconnected after 5' in r.getMessage() for r in recovered.records))
        self.assertEqual(mod._ws_consecutive_errors, 0)

    def test_content_manager_poll_failures_are_deduped(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import scenes_manager_service as mod
        mod._poll_failures.clear()
        with self.assertLogs(level='DEBUG') as logs:
            mod._log_poll_failure('update-poll', 'No response')
            mod._log_poll_failure('update-poll', 'No response')
            mod._log_poll_failure('update-poll', 'No response')
        levels = [r.levelname for r in logs.records]
        self.assertEqual(levels, ['WARNING', 'DEBUG', 'DEBUG'])

        with self.assertLogs(level='DEBUG') as recovered:
            mod._note_poll_success('update-poll')
        self.assertTrue(any('recovered after 3' in r.getMessage() for r in recovered.records))
        self.assertEqual(mod._poll_failures, {})


if __name__ == '__main__':
    unittest.main()
