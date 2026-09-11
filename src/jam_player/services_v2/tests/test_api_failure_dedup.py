"""
Repeated request failures must not keep writing to the SD card.

common.api.api_request logs the FIRST failure of an endpoint at WARNING
(kept on the card), every consecutive repeat at DEBUG (shipped only), and
one INFO line when the endpoint recovers. Runs on-device only (real
`requests`); the network is never touched -- every requests.* call is
patched.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from common import api  # noqa: E402


def _response(status: int) -> mock.MagicMock:
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 300
    return resp


class ApiFailureDedupTests(unittest.TestCase):
    def setUp(self):
        api._reset_failure_state_for_tests()
        # Signing needs device keys; we are testing logging, not signing.
        self._sign = mock.patch.object(api, 'sign_request', return_value={'X-Device-ID': 'test'})
        self._sign.start()
        self.addCleanup(self._sign.stop)

    def _call(self, method='GET', path='/jam-players/heartbeat'):
        return api.api_request(method=method, path=path, body=None, timeout=1, signed=True)

    def test_first_timeout_is_a_warning_and_repeats_are_debug(self):
        with mock.patch.object(api.requests, 'get', side_effect=requests.exceptions.Timeout):
            with self.assertLogs('common.api', level='DEBUG') as first:
                self.assertIsNone(self._call())
            with self.assertLogs('common.api', level='DEBUG') as second:
                self.assertIsNone(self._call())
                self.assertIsNone(self._call())

        self.assertTrue(any(r.levelname == 'WARNING' and 'timed out' in r.getMessage() for r in first.records))
        repeat_levels = {r.levelname for r in second.records if 'timed out' in r.getMessage()}
        self.assertEqual(repeat_levels, {'DEBUG'}, 'repeats must never reach WARNING or above')
        self.assertTrue(any('consecutive failure #3' in r.getMessage() for r in second.records))

    def test_connection_error_follows_the_same_rule(self):
        with mock.patch.object(api.requests, 'post', side_effect=requests.exceptions.ConnectionError('refused')):
            with self.assertLogs('common.api', level='DEBUG') as logs:
                self._call('POST', '/jam-players/logs')
                self._call('POST', '/jam-players/logs')
        levels = [r.levelname for r in logs.records if 'Could not connect' in r.getMessage()]
        self.assertEqual(levels, ['WARNING', 'DEBUG'])

    def test_non_2xx_first_is_warning_then_debug(self):
        with mock.patch.object(api.requests, 'get', return_value=_response(503)):
            with self.assertLogs('common.api', level='DEBUG') as logs:
                self._call()
                self._call()
        levels = [r.levelname for r in logs.records if 'API response: 503' in r.getMessage()]
        self.assertEqual(levels, ['WARNING', 'DEBUG'])

    def test_recovery_logs_one_info_with_the_count_then_resets(self):
        with mock.patch.object(api.requests, 'get', side_effect=requests.exceptions.Timeout):
            with self.assertLogs('common.api', level='DEBUG'):
                for _ in range(4):
                    self._call()
        with mock.patch.object(api.requests, 'get', return_value=_response(200)):
            with self.assertLogs('common.api', level='DEBUG') as recovered:
                self.assertIsNotNone(self._call())
        infos = [r.getMessage() for r in recovered.records if r.levelname == 'INFO']
        self.assertEqual(len(infos), 1)
        self.assertIn('recovered after 4 consecutive failure(s)', infos[0])
        # Counter reset: the next failure is a WARNING again.
        with mock.patch.object(api.requests, 'get', side_effect=requests.exceptions.Timeout):
            with self.assertLogs('common.api', level='DEBUG') as again:
                self._call()
        self.assertTrue(any(r.levelname == 'WARNING' for r in again.records))

    def test_success_without_prior_failures_logs_no_recovery(self):
        with mock.patch.object(api.requests, 'get', return_value=_response(200)):
            with self.assertLogs('common.api', level='DEBUG') as logs:
                self._call()
        self.assertFalse(any('recovered' in r.getMessage() for r in logs.records))

    def test_endpoints_are_tracked_independently(self):
        with mock.patch.object(api.requests, 'get', side_effect=requests.exceptions.Timeout):
            with self.assertLogs('common.api', level='DEBUG') as logs:
                self._call('GET', '/a')
                self._call('GET', '/b')  # first failure of a DIFFERENT endpoint
        warnings = [r.getMessage() for r in logs.records if r.levelname == 'WARNING']
        self.assertEqual(len(warnings), 2)

    def test_signing_failure_stays_an_error(self):
        """A missing identity is a real local fault, not offline noise."""
        with mock.patch.object(api, 'sign_request', return_value=None):
            with self.assertLogs('common.api', level='DEBUG') as logs:
                self.assertIsNone(self._call())
        self.assertTrue(any(r.levelname == 'ERROR' and 'Failed to sign' in r.getMessage() for r in logs.records))


if __name__ == '__main__':
    unittest.main()
