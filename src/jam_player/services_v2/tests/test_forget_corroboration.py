"""
A 'none' connectivity verdict may delete the WiFi profile the customer just
entered -- the one outcome they cannot undo from the app -- so it needs
corroboration that the WiFi is actually to blame.

Two ways it is not: a clock that is not NTP-synced (every probe is a VERIFIED
TLS handshake, so a months-stale fake-hwclock time makes certificates read
"not yet valid" and everything fails), and ethernet holding the default route
(the probes went out the cable, so a dead ethernet uplink fails them). Fielded
7440d2d never forgot a profile at all; this must not regress it.

Imports only common.network, so it runs on a laptop as well as a player.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


class WaitForClockSyncTests(unittest.TestCase):

    def test_already_synced_returns_immediately_with_no_wait_or_nudge(self):
        with mock.patch.object(network, '_clock_is_synced', return_value=True), \
             mock.patch.object(network.subprocess, 'run') as run, \
             mock.patch.object(network.time, 'sleep') as slept:
            self.assertTrue(network.wait_for_clock_sync())
        run.assert_not_called()
        slept.assert_not_called()

    def test_unsynced_then_synced_nudges_chrony_once_and_stops_early(self):
        states = iter([False, False, False, True])
        with mock.patch.object(network, '_clock_is_synced', side_effect=lambda: next(states)), \
             mock.patch.object(network.subprocess, 'run') as run, \
             mock.patch.object(network.time, 'sleep') as slept:
            self.assertTrue(network.wait_for_clock_sync(max_wait=15))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:2], ['chronyc', 'burst'])
        self.assertEqual(slept.call_count, 3, 'stops as soon as chrony syncs')

    def test_never_syncs_gives_up_after_the_budget(self):
        with mock.patch.object(network, '_clock_is_synced', return_value=False), \
             mock.patch.object(network.subprocess, 'run'), \
             mock.patch.object(network.time, 'sleep') as slept:
            self.assertFalse(network.wait_for_clock_sync(max_wait=15))
        self.assertEqual(slept.call_count, 15)

    def test_budget_fits_the_apps_60s_status_window(self):
        self.assertLessEqual(network.CLOCK_SYNC_WAIT_SECONDS, 15)

    def test_chronyc_failure_never_raises(self):
        with mock.patch.object(network, '_clock_is_synced', return_value=False), \
             mock.patch.object(network.subprocess, 'run', side_effect=OSError('no chronyc')), \
             mock.patch.object(network.time, 'sleep'):
            self.assertFalse(network.wait_for_clock_sync(max_wait=1))


class NoneVerdictBlamesWifiTests(unittest.TestCase):

    def test_unsynced_clock_never_blames_the_wifi(self):
        blame, why = network.none_verdict_blames_wifi(clock_synced=False)
        self.assertFalse(blame)
        self.assertIn('clock', why)

    def test_ethernet_default_route_never_blames_the_wifi(self):
        with mock.patch.object(network, 'get_reported_network_status',
                               return_value={'connectionType': 'ethernet', 'ssid': None}):
            blame, why = network.none_verdict_blames_wifi(clock_synced=True)
        self.assertFalse(blame)
        self.assertIn('ethernet', why)

    def test_synced_clock_on_wifi_blames_the_wifi(self):
        with mock.patch.object(network, 'get_reported_network_status',
                               return_value={'connectionType': 'wifi', 'ssid': 'GuestNet'}):
            self.assertEqual(network.none_verdict_blames_wifi(clock_synced=True), (True, ''))

    def test_unknown_route_fails_safe_to_keeping_the_profile(self):
        with mock.patch.object(network, 'get_reported_network_status', side_effect=OSError('nmcli gone')):
            blame, why = network.none_verdict_blames_wifi(clock_synced=True)
        self.assertFalse(blame)
        self.assertIn('default route', why)


if __name__ == '__main__':
    unittest.main()
