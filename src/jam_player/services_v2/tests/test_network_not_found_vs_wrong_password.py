"""
A wrong network NAME and a wrong PASSWORD must reach the app as different
answers.

Found on the bench on 2026-11-16: a made-up network name, typed into the app,
came back as "invalid password". NetworkManager says "Secrets were required"
whenever a first-time join to a secured network does not complete -- whether
the password was wrong or no access point of that name ever answered -- and
connect_to_wifi passed that on as a wrong password.

Now, before blaming the password, the player checks that an access point with
exactly that name is answering (wifi_network_answers). If none is, it reports
"Network not found", which the apps already show as "Can't find that Wi-Fi".
Both statuses were already part of what the player tells the app, so nothing
changes for the apps or for older players.

Seen on a device: BENCH_TESTS.md, case A10.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402

SERVICES_DIR = Path(__file__).resolve().parents[1]
SECRETS = "Error: Connection activation failed: (7) Secrets were required, but not provided."


def _nmcli(listing=None, list_ok=True, rescan_error=None):
    """A stand-in for subprocess.run. `listing` is a list of scan results, one
    per time the scan list is read (each a list of SSID lines as nmcli -t
    prints them)."""
    listings = list(listing or [])
    calls = []

    def run(cmd, *a, **k):
        calls.append(list(cmd))
        if cmd[:4] == ['nmcli', 'device', 'wifi', 'rescan']:
            if rescan_error:
                raise rescan_error
            return mock.Mock(returncode=0, stdout='', stderr='')
        if 'list' in cmd:
            if not list_ok:
                return mock.Mock(returncode=1, stdout='', stderr='nmcli unavailable')
            lines = listings.pop(0) if listings else []
            return mock.Mock(returncode=0, stdout='\n'.join(lines) + '\n', stderr='')
        raise AssertionError(f'unexpected command {cmd}')

    return run, calls


class DoesAnAccessPointAnswer(unittest.TestCase):

    def _ask(self, ssid, **kw):
        run, calls = _nmcli(**kw)
        with mock.patch.object(network.subprocess, 'run', side_effect=run), \
             mock.patch.object(network.time, 'sleep') as slept:
            answer = network.wifi_network_answers(ssid)
        return answer, calls, slept

    def test_a_network_in_the_scan_list_answers_and_nothing_else_is_run(self):
        answer, calls, slept = self._ask('Shop', listing=[['Other', 'Shop']])
        self.assertTrue(answer)
        self.assertEqual(len(calls), 1, 'no rescan and no wait for the common case')
        slept.assert_not_called()

    def test_the_list_is_read_without_starting_a_new_scan(self):
        _, calls, _ = self._ask('Shop', listing=[['Shop']])
        self.assertEqual(calls[0], ['nmcli', '-t', '-f', 'SSID', 'device', 'wifi', 'list', '--rescan', 'no'])

    def test_a_name_nobody_answers_to_is_not_there(self):
        answer, calls, slept = self._ask('MadeUpName', listing=[['Shop'], ['Shop']])
        self.assertFalse(answer)
        # It was asked for by name once more before giving up.
        self.assertIn(['nmcli', 'device', 'wifi', 'rescan', 'ssid', 'MadeUpName'], calls)
        slept.assert_called_once_with(network._PRESENCE_RESCAN_WAIT_SECONDS)

    def test_a_hidden_network_that_answers_the_scan_by_name_is_there(self):
        answer, _, _ = self._ask('HiddenNet', listing=[['Shop'], ['Shop', 'HiddenNet']])
        self.assertTrue(answer)

    def test_the_match_is_exact_not_a_prefix_or_a_different_case(self):
        for seen in (['Shop-5G'], ['shop'], ['Shop '], ['My Shop']):
            answer, _, _ = self._ask('Shop', listing=[seen, seen])
            self.assertFalse(answer, seen)

    def test_a_name_with_a_colon_is_read_correctly(self):
        answer, _, _ = self._ask('Cafe: Guest', listing=[['Cafe\\: Guest']])
        self.assertTrue(answer)

    def test_a_scan_that_cannot_be_requested_still_reads_the_list(self):
        answer, _, _ = self._ask('Late', listing=[[], ['Late']], rescan_error=OSError('radio busy'))
        self.assertTrue(answer)

    def test_if_networkmanager_cannot_be_asked_the_old_verdict_stands(self):
        answer, _, _ = self._ask('Anything', list_ok=False)
        self.assertTrue(answer, 'unknown must not turn a wrong password into "not found"')


class WhatTheAppIsTold(unittest.TestCase):

    def _connect(self, ssid, stderr, answers, previous=None):
        failed = mock.MagicMock(returncode=1, stdout='', stderr=stderr)
        order = []
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=previous), \
             mock.patch.object(network, 'check_nm_connection_state', return_value=(False, 'x')), \
             mock.patch.object(network, '_log_network_diagnostic_info'), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network, '_connect_wifi_secure', return_value=failed), \
             mock.patch.object(network, '_restore_wifi_connection',
                               side_effect=lambda name: order.append('restore')), \
             mock.patch.object(network, 'wifi_network_answers',
                               side_effect=lambda s: order.append('check') or answers) as check:
            result = network.connect_to_wifi(ssid, 'pw', hidden=True)
        return result, check, order

    def test_a_made_up_name_is_network_not_found(self):
        result, check, _ = self._connect('MadeUpName', SECRETS, answers=False)
        self.assertEqual(result, (False, 'Network not found'))
        check.assert_called_once_with('MadeUpName')

    def test_a_wrong_password_for_a_real_network_is_still_invalid_password(self):
        result, _, _ = self._connect('Shop', SECRETS, answers=True)
        self.assertEqual(result, (False, 'Invalid password'))

    def test_the_working_network_is_restored_before_the_check(self):
        # The check can take a few seconds; the player must already be on its
        # way back to the network that works.
        _, _, order = self._connect('MadeUpName', SECRETS, answers=False,
                                    previous={'name': 'jam-wifi-good', 'ssid': 'Good'})
        self.assertEqual(order, ['restore', 'check'])

    def test_other_failures_are_reported_as_before_without_the_check(self):
        result, check, _ = self._connect('Shop', 'Error: No network with SSID \'Shop\' found.', answers=True)
        self.assertEqual(result, (False, 'Network not found'))
        check.assert_not_called()
        result, check, _ = self._connect('Shop', 'Error: something else', answers=True)
        self.assertEqual(result, (False, 'Error: something else'))
        check.assert_not_called()

    def test_a_successful_join_never_runs_the_check(self):
        ok = mock.MagicMock(returncode=0, stdout='', stderr='')
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=None), \
             mock.patch.object(network, '_log_network_diagnostic_info'), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network, '_connect_wifi_secure', return_value=ok), \
             mock.patch.object(network, '_promote_in_background'), \
             mock.patch.object(network, 'wifi_network_answers') as check:
            self.assertEqual(network.connect_to_wifi('Shop', 'pw'), (True, ''))
        check.assert_not_called()


NOT_IN_RANGE = ("Error: Connection activation failed: The Wi-Fi network could not be found\n"
                "Hint: use 'journalctl -xe NM_CONNECTION=0344bb06 + NM_DEVICE=wlan0' to get more details.")


class ASavedNetworkThatIsNotThere(unittest.TestCase):
    """Bench, 2026-11-16: the phone hotspot the player had saved was no longer
    advertising. NetworkManager said "The Wi-Fi network could not be found";
    the player passed that on word for word, the status mapping did not
    recognise it, and the app was told 'failed'."""

    def _saved(self, returncode, stderr, previous=None):
        result = mock.MagicMock(returncode=returncode, stdout='', stderr=stderr)
        restored = []
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=previous), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network.subprocess, 'run', return_value=result), \
             mock.patch.object(network, '_restore_wifi_connection', side_effect=restored.append), \
             mock.patch.object(network, '_promote_in_background'):
            answer = network.connect_to_saved_wifi('jam-wifi-428d1ae5')
        return answer, restored

    def test_a_saved_network_out_of_range_is_network_not_found(self):
        answer, _ = self._saved(4, NOT_IN_RANGE)
        self.assertEqual(answer, (False, 'Network not found'))

    def test_the_network_that_was_working_is_still_restored(self):
        _, restored = self._saved(4, NOT_IN_RANGE, previous={'name': 'jam-wifi-bf216a93', 'ssid': 'Good'})
        self.assertEqual(restored, ['jam-wifi-bf216a93'])

    def test_other_saved_network_failures_are_passed_on_as_before(self):
        answer, _ = self._saved(4, 'Error: Connection activation failed: Secrets were required, but not provided.')
        self.assertEqual(answer, (False, 'Error: Connection activation failed: Secrets were required, but not provided.'))

    def test_a_saved_network_that_connects_is_unaffected(self):
        answer, _ = self._saved(0, '')
        self.assertEqual(answer, (True, ''))

    def test_both_networkmanager_wordings_are_recognised(self):
        self.assertTrue(network._says_network_not_found("Error: No network with SSID 'Shop' found."))
        self.assertTrue(network._says_network_not_found(NOT_IN_RANGE))
        for other in ('', None, 'Secrets were required, but not provided', 'timeout', 'Connection failed'):
            self.assertFalse(network._says_network_not_found(other), other)

    def test_a_typed_network_gets_the_same_answer_for_that_wording(self):
        failed = mock.MagicMock(returncode=4, stdout='', stderr=NOT_IN_RANGE)
        with mock.patch.object(network, '_get_active_wifi_connection', return_value=None), \
             mock.patch.object(network, '_log_network_diagnostic_info'), \
             mock.patch.object(network, '_stop_comitup_hotspot', return_value=False), \
             mock.patch.object(network, '_connect_wifi_secure', return_value=failed), \
             mock.patch.object(network, 'wifi_network_answers') as check:
            self.assertEqual(network.connect_to_wifi('Gone', 'pw'), (False, 'Network not found'))
        check.assert_not_called()


class TheStatusCodesTheAppsAlreadyKnow(unittest.TestCase):
    """jam_ble_provisioning turns the text above into a status code by looking
    for words in it. It cannot be imported off-device (D-Bus), so this pins the
    words against its source."""

    def test_each_verdict_maps_to_its_own_existing_status(self):
        source = (SERVICES_DIR / 'jam_ble_provisioning.py').read_text()
        password_at = source.index("if 'password' in error_msg.lower() or 'secrets' in error_msg.lower():")
        not_found_at = source.index("elif 'not found' in error_msg.lower() or 'no network' in error_msg.lower():")
        self.assertLess(password_at, not_found_at)
        self.assertIn("status_code = 'invalid_password'", source[password_at:not_found_at])
        self.assertIn("status_code = 'network_not_found'", source[not_found_at:not_found_at + 200])
        # "Network not found" must not be caught by the password branch first.
        text = 'Network not found'.lower()
        self.assertNotIn('password', text)
        self.assertNotIn('secrets', text)
        self.assertIn('not found', text)
        self.assertIn('password', 'Invalid password'.lower())


if __name__ == '__main__':
    unittest.main()
