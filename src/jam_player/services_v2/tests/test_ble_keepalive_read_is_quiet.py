"""
The apps' keep-alive must not write to the SD card.

Both apps read the BLE Device Info characteristic every 3 seconds for as long
as they are connected, without subscribing to it. The player answers each read
with a short "subscribe first" note. That was logged as a WARNING, and the card
keeps WARNING and above, so every setup session wrote a line every 3 seconds
and buried the lines that matter (bench, 2026-11-16: 26 of the last 30 lines).

It is logged at DEBUG now. What the app is sent does not change.

Imports the BLE service, so it runs on a player only.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import jam_ble_provisioning as ble  # noqa: E402
    WHY_NOT = ''
except ImportError as exc:  # no D-Bus modules off-device
    ble = None
    WHY_NOT = f"needs the player's D-Bus modules: {exc}"


def _read(characteristic_class):
    chrc = object.__new__(characteristic_class)
    chrc._notifying = False
    return bytes(bytearray(chrc.ReadValue({})))


@unittest.skipIf(ble is None, WHY_NOT)
class KeepAliveReadIsQuiet(unittest.TestCase):

    def test_a_device_info_read_without_a_subscription_logs_no_warning(self):
        with self.assertNoLogs(ble.logger, level='WARNING'):
            _read(ble.DeviceInfoCharacteristic)

    def test_the_app_still_gets_the_same_reply(self):
        reply = json.loads(_read(ble.DeviceInfoCharacteristic).decode('utf-8'))
        self.assertEqual(reply, {"error": "Subscribe to notifications first"})

    def test_it_is_still_visible_at_debug(self):
        with self.assertLogs(ble.logger, level='DEBUG') as logs:
            _read(ble.DeviceInfoCharacteristic)
        self.assertTrue(any('notifications not enabled' in line for line in logs.output))


@unittest.skipIf(ble is None, WHY_NOT)
class PostAnnounceRestartsDoNotWait(unittest.TestCase):
    """After a WiFi change the player restarts the services that need the new
    network. Waiting for each one to report ready timed out for two of them
    and logged a failure for a restart that then succeeded."""

    def _restarts(self, run_side_effect=None):
        from unittest import mock
        with mock.patch('subprocess.run', side_effect=run_side_effect) as run:
            ble._trigger_post_announce_services()
        return [call.args[0] for call in run.call_args_list]

    def test_every_restart_is_queued_not_waited_for(self):
        commands = self._restarts()
        self.assertTrue(commands)
        for command in commands:
            self.assertEqual(command[:3], ['systemctl', 'restart', '--no-block'], command)

    def test_the_same_services_are_restarted_as_before(self):
        self.assertEqual([command[-1] for command in self._restarts()], [
            'jam-tailscale.service', 'jam-ws-commands.service',
            'jam-heartbeat.service', 'jam-registration-poller.timer'])

    def test_one_failure_does_not_stop_the_others(self):
        self.assertEqual(len(self._restarts(run_side_effect=OSError('no systemctl'))), 4)


if __name__ == '__main__':
    unittest.main()
