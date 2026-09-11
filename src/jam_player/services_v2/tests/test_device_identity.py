"""
The identity block on every non-content screen must print the SAME five
characters the phone shows in its Bluetooth list, or users cannot match a
screen to a device. Pure text -- no rendering -- so it runs anywhere the
services do.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import credentials  # noqa: E402

UUID = '3f2a9c10-7b4e-4d2a-9e1f-0a1b2c3d4e5f'


class DeviceIdentityTests(unittest.TestCase):
    def test_short_id_matches_what_the_apps_compute(self):
        """Android: deviceUuid.takeLast(5).uppercase(); iOS: suffix(5).uppercased()."""
        with mock.patch.object(credentials, 'get_device_uuid', return_value=UUID):
            self.assertEqual(credentials.get_device_uuid_short(5), UUID[-5:].upper())

    def test_ble_name_is_prefix_plus_short_id(self):
        with mock.patch.object(credentials, 'get_device_uuid', return_value=UUID):
            self.assertEqual(credentials.get_ble_device_name(), 'JAM-PLAYER-D4E5F')

    def test_ble_name_without_a_uuid_is_the_placeholder(self):
        with mock.patch.object(credentials, 'get_device_uuid', return_value=None):
            self.assertEqual(credentials.get_ble_device_name(), 'JAM-PLAYER-XXXXX')

    def test_identity_lines_say_both_things_and_keep_the_full_uuid(self):
        lines = credentials.device_identity_lines(UUID)
        self.assertEqual(lines, [
            'Device ID: D4E5F',
            'Setup network: JAM-PLAYER-D4E5F',
            f'Device: {UUID}',
        ])

    def test_identity_lines_use_the_same_suffix_as_the_ble_name(self):
        """The two must never drift: one is what the screen says, the other
        is what the phone sees."""
        with mock.patch.object(credentials, 'get_device_uuid', return_value=UUID):
            ble = credentials.get_ble_device_name()
        self.assertIn(f'Setup network: {ble}', credentials.device_identity_lines(UUID))

    def test_lowercase_uuid_is_uppercased_for_display(self):
        lines = credentials.device_identity_lines('abcdef00-0000-0000-0000-00000000abcde')
        self.assertEqual(lines[0], 'Device ID: ABCDE')

    def test_no_uuid_means_no_identity_block(self):
        self.assertEqual(credentials.device_identity_lines(None), [])
        self.assertEqual(credentials.device_identity_lines(''), [])


class ScreenRenderSmokeTests(unittest.TestCase):
    """Every non-content screen must still render with the identity block.
    Imports the display module (GTK/PIL), so this runs on a player only."""

    SCREENS = [
        'create_unregistered_screen',
        'create_waiting_for_content_screen',
        'create_awaiting_screen_link_screen',
        'create_awaiting_registration_screen',
        'create_no_active_scenes_screen',
        'create_no_scheduled_content_screen',
        'create_outlet_inactive_screen',
    ]

    def _render(self, name, uuid):
        import jam_player_display as display
        result = getattr(display, name)(1920, 1080, uuid)
        img = result[0] if isinstance(result, tuple) else result
        self.assertEqual(img.size, (1920, 1080), name)
        return img

    def test_every_status_screen_renders_with_and_without_a_uuid(self):
        for name in self.SCREENS:
            self._render(name, UUID)
            self._render(name, None)

    def test_identity_block_changes_the_pixels_at_the_bottom(self):
        """Crude but real: the bottom band differs with vs without a UUID."""
        for name in self.SCREENS:
            with_id = self._render(name, UUID).crop((0, 900, 1920, 1060)).tobytes()
            without = self._render(name, None).crop((0, 900, 1920, 1060)).tobytes()
            self.assertNotEqual(with_id, without, f'{name} draws nothing for the device identity')


if __name__ == '__main__':
    unittest.main()
