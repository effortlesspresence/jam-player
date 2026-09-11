"""
Autoconnect priority: the network the user most recently connected to must
be NetworkManager's top autoconnect candidate; everyone else keeps their
relative order beneath it. Bookkeeping is best-effort and never raises.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402


class FakeNmcli:
    """
    Answers the three nmcli shapes promote_wifi_connection_priority uses:
      connection show                         -> NAME:TYPE listing
      connection show <name> (-f priority)    -> 'connection.autoconnect-priority:N'
      connection modify <name> ... <N>        -> records the write
    """

    def __init__(self, profiles, fail_modify=False):
        # profiles: {name: priority}; non-wifi profiles included as ('eth', None)
        self.profiles = dict(profiles)
        self.fail_modify = fail_modify
        self.modified = []  # (name, priority) in call order

    def __call__(self, argv, **kwargs):
        cp = subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        if argv[:2] == ['nmcli', '-t'] and 'NAME,TYPE,AUTOCONNECT-PRIORITY' in argv:
            lines = []
            for name, prio in self.profiles.items():
                ctype = '802-3-ethernet' if prio is None else '802-11-wireless'
                esc = name.replace('\\', '\\\\').replace(':', '\\:')
                lines.append(f'{esc}:{ctype}:{prio or 0}')
            cp.stdout = '\n'.join(lines) + '\n'
            return cp
        if argv[:3] == ['nmcli', 'connection', 'modify']:
            name, prio = argv[3], int(argv[-1])
            if self.fail_modify:
                return subprocess.CompletedProcess(argv, 1, stdout='', stderr='Error: denied')
            self.profiles[name] = prio
            self.modified.append((name, prio))
            return cp
        raise AssertionError(f'unexpected nmcli call: {argv}')


class PromotePriorityTests(unittest.TestCase):
    def _promote(self, profiles, name, **kw):
        fake = FakeNmcli(profiles, **kw)
        with mock.patch.object(network.subprocess, 'run', side_effect=fake):
            network.promote_wifi_connection_priority(name)
        return fake

    def test_newly_connected_network_goes_above_every_other(self):
        fake = self._promote({'home': 0, 'shop': 5, 'guest': 2, 'wired': None}, 'home')
        self.assertEqual(fake.modified, [('home', 6)])
        self.assertEqual(fake.profiles['home'], 6)
        # Others untouched -> relative order preserved.
        self.assertEqual((fake.profiles['shop'], fake.profiles['guest']), (5, 2))

    def test_reselecting_current_top_stays_top_with_one_write(self):
        fake = self._promote({'a': 0, 'b': 5, 'c': 2}, 'b')
        self.assertEqual(fake.modified, [('b', 3)])  # above max(others)=2

    def test_names_with_colons_round_trip_through_nmcli_escaping(self):
        fake = self._promote({'Cafe: Guest': 0, 'other': 4}, 'Cafe: Guest')
        self.assertEqual(fake.modified, [('Cafe: Guest', 5)])

    def test_ethernet_profiles_are_ignored(self):
        fake = self._promote({'wired': None, 'only': 0}, 'only')
        self.assertEqual(fake.modified, [('only', 1)])

    def test_renumbers_compactly_at_ceiling_preserving_order(self):
        ceiling = network._AUTOCONNECT_PRIORITY_CEILING
        fake = self._promote({'old': ceiling, 'mid': 10, 'low': 3, 'new': 0}, 'new')
        # others sorted ascending: low, mid, old -> 0,1,2 ; new -> 3
        self.assertEqual(fake.modified, [('low', 0), ('mid', 1), ('old', 2), ('new', 3)])

    def test_modify_failure_never_raises(self):
        fake = self._promote({'a': 0, 'b': 1}, 'a', fail_modify=True)
        self.assertEqual(fake.modified, [])

    def test_empty_name_is_a_noop(self):
        with mock.patch.object(network.subprocess, 'run', side_effect=AssertionError('no nmcli')):
            network.promote_wifi_connection_priority('')

    def test_nmcli_exception_never_raises(self):
        with mock.patch.object(network.subprocess, 'run', side_effect=OSError('nmcli missing')):
            network.promote_wifi_connection_priority('x')


class ConnectPathsPromoteTests(unittest.TestCase):
    """Every successful user-driven connect promotes the resulting profile."""

    def test_saved_network_success_promotes_that_profile(self):
        ok = subprocess.CompletedProcess(['nmcli'], 0, stdout='', stderr='')
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=None), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network.subprocess, 'run', return_value=ok), \
             mock.patch.object(network, '_promote_in_background') as promote:
            self.assertEqual(network.connect_to_saved_wifi('cafe'), (True, ''))
        promote.assert_called_once()  # background promotion of 'cafe'

    def test_saved_network_failure_does_not_promote(self):
        bad = subprocess.CompletedProcess(['nmcli'], 4, stdout='', stderr='no secrets')
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=None), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network.subprocess, 'run', return_value=bad), \
             mock.patch.object(network, '_promote_in_background') as promote:
            ok, _ = network.connect_to_saved_wifi('cafe')
        self.assertFalse(ok)
        promote.assert_not_called()

    def test_new_network_success_promotes_the_active_profile(self):
        ok = subprocess.CompletedProcess(['nmcli'], 0, stdout='', stderr='')
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=None), \
             mock.patch.object(network, '_log_network_diagnostic_info'), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network, '_connect_wifi_secure', return_value=ok), \
             mock.patch.object(network, '_promote_in_background') as promote:
            self.assertEqual(network.connect_to_wifi('Cafe', 'pw'), (True, ''))
        promote.assert_called_once()

    def test_already_connected_reselection_still_promotes(self):
        current = {'name': 'jam-wifi-abc', 'ssid': 'Cafe'}
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=current), \
             mock.patch.object(network, 'check_nm_connection_state', return_value=(True, 'wifi')), \
             mock.patch.object(network, '_promote_in_background') as promote:
            self.assertEqual(network.connect_to_wifi('Cafe', 'pw'), (True, ''))
        promote.assert_called_once()


if __name__ == '__main__':
    unittest.main()
