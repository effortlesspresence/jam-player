"""
A phone that is merely still linked must not keep Bluetooth setup open.

Found on the bench on 2026-11-16 (player on 8d8cf18, iPhone, app 1.6.0 b4):

    18:12:37  Bluetooth setup starts (boot)
    18:27:37  the 15-minute window ends
    18:30:14  the app lets go of the player (its last keep-alive read)
    18:33:39  `bluetoothctl devices Connected` still lists the phone, and
              jam-ble-provisioning is still running

The state manager holds setup open for a connected phone so that a setup in
progress is never cut off. It asked BlueZ "is a phone connected?" and trusted
that the answer would clear when the app was done. iOS keeps the link up for
minutes after the app releases it, so the answer stayed yes and setup stayed
open with nobody using it -- for as long as that phone stayed in range.

Now a connected phone counts as a setup in progress only while it is USING
the link. jam-ble-provisioning touches a marker on every read and write, both
apps read Device Info every 3 seconds while they are on a setup page, and the
state manager ignores a link that has been silent for two minutes. When setup
then closes, the idle link is dropped.

The state-manager half runs anywhere its imports resolve (a player, or a
machine with stand-ins for dbus/gi). The last class reads the BLE service's
source, so it runs everywhere.
"""
import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import jam_ble_state_manager as sm  # noqa: E402
    WHY_NOT = ''
except ImportError as exc:  # no dbus / gi off-device
    sm = None
    WHY_NOT = f"needs the player's D-Bus modules: {exc}"

SERVICES_DIR = Path(__file__).resolve().parents[1]
PHONE = '/org/bluez/hci0/dev_08_24_0B_B4_DC_47'
NOW = 1_000_000.0


def _manager(connected_paths=(PHONE,)):
    """A BLEStateManager with a fake BlueZ that reports these devices linked."""
    m = object.__new__(sm.BLEStateManager)
    m._recovery_window_closed_logged = False
    m._session_hold_logged = False
    m._pending_action = None
    m._internet_check_timer = None
    m._first_connect_update_triggered = False
    m.bus = mock.MagicMock()
    m.devices = {path: mock.MagicMock(name=path) for path in connected_paths}
    objects = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Powered': True}}}
    objects.update({path: {'org.bluez.Device1': {'Connected': True}} for path in connected_paths})
    objects['/org/bluez/hci0/dev_AA_AA'] = {'org.bluez.Device1': {'Connected': False}}

    def interface(obj, name):
        if name == 'org.freedesktop.DBus.ObjectManager':
            om = mock.MagicMock()
            om.GetManagedObjects.return_value = objects
            return om
        return m.devices[obj]

    m.bus.get_object.side_effect = lambda service, path: path
    m._interface = interface
    return m


def _flag(age_seconds=None, unreadable=False):
    flag = mock.MagicMock()
    flag.exists.return_value = age_seconds is not None or unreadable
    if unreadable:
        flag.stat.side_effect = OSError('EIO')
    elif age_seconds is not None:
        flag.stat.return_value = mock.MagicMock(st_mtime=NOW - age_seconds)
    return flag


def _session(m, activity_age, wifi_join_age=None, unreadable=False):
    with mock.patch.object(sm, 'BLE_CLIENT_ACTIVITY_FLAG', _flag(activity_age, unreadable)), \
         mock.patch.object(sm, 'BLE_SESSION_ACTIVE_FLAG', _flag(wifi_join_age)), \
         mock.patch.object(sm.dbus, 'Interface', side_effect=m._interface), \
         mock.patch.object(sm.time, 'time', return_value=NOW):
        return m._setup_session_in_progress()


@unittest.skipIf(sm is None, WHY_NOT)
class WhoCountsAsASetupInProgress(unittest.TestCase):

    def test_an_app_that_is_talking_to_the_player_is_a_session(self):
        # The keep-alive is every 3 s; allow for a few missed ones.
        for age in (0, 3, 30, sm.BLE_CLIENT_ACTIVITY_MAX_AGE_SECONDS - 1):
            self.assertTrue(_session(_manager(), activity_age=age), age)

    def test_the_bench_case_a_phone_still_linked_but_silent_is_not(self):
        # 18:30:14 last read, 18:33:39 still linked: 205 seconds of silence.
        self.assertFalse(_session(_manager(), activity_age=205))

    def test_silence_just_over_the_limit_is_not(self):
        self.assertFalse(_session(_manager(), activity_age=sm.BLE_CLIENT_ACTIVITY_MAX_AGE_SECONDS + 1))

    def test_a_phone_that_just_connected_and_has_read_nothing_yet_is_a_session(self):
        # No marker: a fresh link, or a BLE service on code that does not
        # write one. Either way the old rule is the safe one.
        self.assertTrue(_session(_manager(), activity_age=None))

    def test_a_marker_that_cannot_be_read_does_not_cut_a_session_off(self):
        self.assertTrue(_session(_manager(), activity_age=None, unreadable=True))

    def test_recent_activity_with_no_phone_linked_is_not_a_session(self):
        self.assertFalse(_session(_manager(connected_paths=()), activity_age=1))

    def test_a_wifi_join_in_flight_is_a_session_whatever_the_link_is_doing(self):
        self.assertTrue(_session(_manager(), activity_age=500, wifi_join_age=10))
        self.assertTrue(_session(_manager(connected_paths=()), activity_age=None, wifi_join_age=10))

    def test_the_limit_leaves_a_live_app_plenty_of_room(self):
        keep_alive_seconds = 3
        self.assertGreaterEqual(sm.BLE_CLIENT_ACTIVITY_MAX_AGE_SECONDS, 30 * keep_alive_seconds)
        # ...and is short enough that the window really closes.
        self.assertLessEqual(sm.BLE_CLIENT_ACTIVITY_MAX_AGE_SECONDS, 5 * 60)


@unittest.skipIf(sm is None, WHY_NOT)
class TheWindowReallyCloses(unittest.TestCase):
    WINDOW = sm.BLE_BOOT_RECOVERY_WINDOW_SECONDS if sm else 0

    def _should_run(self, m, activity_age, uptime):
        with mock.patch.object(sm, 'BLE_CLIENT_ACTIVITY_FLAG', _flag(activity_age)), \
             mock.patch.object(sm, 'BLE_SESSION_ACTIVE_FLAG', _flag(None)), \
             mock.patch.object(sm.dbus, 'Interface', side_effect=m._interface), \
             mock.patch.object(sm.time, 'time', return_value=NOW), \
             mock.patch.object(sm, 'seconds_since_boot', return_value=uptime), \
             mock.patch.object(sm, 'unit_active_since_boot_seconds', return_value=None), \
             mock.patch.object(sm, 'is_device_registered', return_value=True), \
             mock.patch.object(sm, 'api_recently_ok', return_value=True):
            return m._should_ble_run(True, 'jam_backend')

    def test_with_an_idle_phone_linked_setup_closes_after_the_window(self):
        self.assertFalse(self._should_run(_manager(), activity_age=205, uptime=self.WINDOW + 360))

    def test_with_an_app_in_a_session_it_stays_open_past_the_window(self):
        self.assertTrue(self._should_run(_manager(), activity_age=2, uptime=self.WINDOW + 360))

    def test_inside_the_window_nothing_changes(self):
        self.assertTrue(self._should_run(_manager(), activity_age=205, uptime=self.WINDOW - 60))


@unittest.skipIf(sm is None, WHY_NOT)
class IdleLinksAreDroppedWhenSetupCloses(unittest.TestCase):

    def test_every_linked_phone_is_disconnected_and_only_those(self):
        other = '/org/bluez/hci0/dev_11_22'
        m = _manager(connected_paths=(PHONE, other))
        with mock.patch.object(sm.dbus, 'Interface', side_effect=m._interface):
            m._disconnect_idle_ble_links()
        m.devices[PHONE].Disconnect.assert_called_once_with()
        m.devices[other].Disconnect.assert_called_once_with()

    def test_nothing_linked_means_nothing_to_do(self):
        m = _manager(connected_paths=())
        with mock.patch.object(sm.dbus, 'Interface', side_effect=m._interface):
            m._disconnect_idle_ble_links()  # must not raise

    def test_one_failure_does_not_stop_the_rest_or_raise(self):
        other = '/org/bluez/hci0/dev_11_22'
        m = _manager(connected_paths=(PHONE, other))
        m.devices[PHONE].Disconnect.side_effect = Exception('org.bluez.Error.Failed')
        with mock.patch.object(sm.dbus, 'Interface', side_effect=m._interface):
            m._disconnect_idle_ble_links()
        m.devices[other].Disconnect.assert_called_once_with()

    def test_bluez_being_unreachable_never_raises(self):
        m = _manager()
        m.bus.get_object.side_effect = Exception('no bluez')
        with mock.patch.object(sm.dbus, 'Interface', side_effect=Exception('no bluez')):
            m._disconnect_idle_ble_links()

    def test_it_is_only_ever_called_on_the_path_that_stops_setup(self):
        # A phone in a session must never be disconnected by us. The one call
        # sits where the periodic check has just decided to stop setup.
        source = (SERVICES_DIR / 'jam_ble_state_manager.py').read_text()
        self.assertEqual(source.count('self._disconnect_idle_ble_links()'), 1)
        call_at = source.index('self._disconnect_idle_ble_links()')
        decided_at = source.rindex('if not should_run and not self._recovery_window_closed_logged:', 0, call_at)
        between = source[decided_at:call_at]
        self.assertNotIn('\n        else:', between)
        self.assertNotIn('\n        elif', between)


class TheBleServiceMarksEveryReadAndWrite(unittest.TestCase):
    """jam_ble_provisioning cannot be imported off-device, so read its source."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((SERVICES_DIR / 'jam_ble_provisioning.py').read_text())

    def _handlers(self):
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name != 'Characteristic':
                for fn in node.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name in ('ReadValue', 'WriteValue'):
                        yield node.name, fn

    def test_every_characteristic_the_apps_use_is_covered(self):
        seen = {(cls, fn.name) for cls, fn in self._handlers()}
        self.assertEqual(seen, {
            ('WiFiNetworksCharacteristic', 'ReadValue'),
            ('SavedNetworksCharacteristic', 'ReadValue'),
            ('WiFiCredentialsCharacteristic', 'WriteValue'),
            ('ConnectionStatusCharacteristic', 'ReadValue'),
            ('DeviceInfoCharacteristic', 'ReadValue'),   # the apps' 3 s keep-alive
            ('ProvisioningConfirmCharacteristic', 'WriteValue'),
            ('ScreenIdCharacteristic', 'WriteValue'),
        })

    def test_each_one_notes_the_activity_before_doing_anything_else(self):
        for cls, fn in self._handlers():
            first = fn.body[1]  # body[0] is the docstring
            self.assertIsInstance(first, ast.Expr, f'{cls}.{fn.name}')
            self.assertIsInstance(first.value, ast.Call, f'{cls}.{fn.name}')
            self.assertEqual(ast.unparse(first.value), '_note_client_activity()', f'{cls}.{fn.name}')

    def test_the_marker_is_a_tmpfs_touch_not_a_card_write(self):
        source = (SERVICES_DIR / 'jam_ble_provisioning.py').read_text()
        helper = source[source.index('def _note_client_activity():'):source.index('class Characteristic(')]
        self.assertIn('touch_volatile_flag(BLE_CLIENT_ACTIVITY_FLAG)', helper)
        self.assertNotIn('safe_touch', helper)
        paths = (SERVICES_DIR / 'common' / 'paths.py').read_text()
        self.assertIn("BLE_CLIENT_ACTIVITY_FLAG = JAM_RUN_DIR / 'ble_client_activity'", paths)
        self.assertIn("JAM_RUN_DIR = Path('/run/jam')", paths)


if __name__ == '__main__':
    unittest.main()
