"""
The periodic (15 s) connectivity probe must not be an API call, and API health must
come from real signed calls.

Before: check_internet_connectivity() step 1 was requests.get(/jam-players/
health) -- ~12,300 billed API Gateway requests per player per day, ~92% of
everything the fleet sent. After: step 1 is a VERIFIED TLS handshake to our
backend host (proves DNS + network path + our certificate; portal-proof; sends
no request, so it is not an API call). API health is a different question,
answered by API_LAST_OK_FLAG, stamped by every successful signed call
(heartbeat, announce) and read via api_recently_ok().

The core tests import only common.network and run on a laptop; the two that
need common.api (requests, nacl) run on a JAM Player.
"""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import network  # noqa: E402

BACKEND = ('api.example.test', 443)


def _api_importable() -> bool:
    try:
        spec = importlib.util.find_spec('requests')
    except Exception:
        return False
    return spec is not None


class PeriodicProbeIsATlsHandshakeTests(unittest.TestCase):

    def _probe(self, tls_results):
        """tls_results: {(host, port): bool}. Records call order."""
        calls = []
        def fake_tls(host, port, timeout):
            calls.append((host, port))
            return tls_results.get((host, port), False)
        with mock.patch.object(network, '_backend_endpoint', return_value=BACKEND), \
             mock.patch.object(network, '_check_tls_connectivity', side_effect=fake_tls):
            result = network.check_internet_connectivity(timeout=1.0)
        return result, calls

    def test_step_one_is_a_tls_handshake_to_our_backend_host(self):
        result, calls = self._probe({BACKEND: True})
        self.assertEqual(result, (True, 'jam_backend'))
        self.assertEqual(calls[0], BACKEND, 'our backend must be probed FIRST, by TLS')
        self.assertEqual(len(calls), 1, 'a reachable backend needs no fallback probes')

    def test_backend_edge_unreachable_falls_back_to_public_tls(self):
        result, calls = self._probe({('1.1.1.1', 443): True})
        self.assertEqual(result, (True, 'cloudflare_tls'))
        self.assertEqual(calls[0], BACKEND)

    def test_nothing_verifiable_is_offline(self):
        result, _ = self._probe({})
        self.assertEqual(result, (False, 'none'))

    def test_unparseable_base_url_skips_straight_to_fallbacks(self):
        calls = []
        def fake_tls(host, port, timeout):
            calls.append((host, port)); return host == '8.8.8.8'
        with mock.patch.object(network, '_backend_endpoint', return_value=None), \
             mock.patch.object(network, '_check_tls_connectivity', side_effect=fake_tls):
            self.assertEqual(network.check_internet_connectivity(timeout=1.0), (True, 'google_tls'))
        self.assertNotIn(BACKEND, calls)

    def test_parse_backend_endpoint_default_port(self):
        self.assertEqual(network._parse_backend_endpoint('https://api.example.test/v2'),
                         ('api.example.test', 443))

    def test_parse_backend_endpoint_explicit_port(self):
        self.assertEqual(network._parse_backend_endpoint('https://staging.example.test:8443'),
                         ('staging.example.test', 8443))

    def test_parse_backend_endpoint_garbage_is_none(self):
        self.assertIsNone(network._parse_backend_endpoint('not a url'))
        self.assertIsNone(network._parse_backend_endpoint(''))


@unittest.skipUnless(_api_importable(), 'needs the device venv (requests); runs on a JAM Player')
class NoHttpOnThePeriodicPathTests(unittest.TestCase):

    def test_periodic_probe_never_issues_an_http_request(self):
        with mock.patch.object(network, '_backend_endpoint', return_value=BACKEND), \
             mock.patch.object(network, '_check_tls_connectivity', return_value=True), \
             mock.patch('common.api.check_api_availability',
                        side_effect=AssertionError('the 7 s probe must not GET /jam-players/health')):
            self.assertEqual(network.check_internet_connectivity(timeout=1.0), (True, 'jam_backend'))

    def test_connect_flow_classifier_still_checks_real_api_health(self):
        """classify_connectivity is event-driven (once per connect attempt) and
        must distinguish 'firewall blocks us' from 'we are down' -> keeps HTTP."""
        with mock.patch('common.api.check_api_availability', return_value=True) as http:
            self.assertEqual(network.classify_connectivity(attempts=1, delay=0, timeout=1.0), 'backend')
        http.assert_called()

    def test_backend_endpoint_uses_the_configured_base_url(self):
        with mock.patch('common.api.get_api_base_url', return_value='https://api.example.test'):
            self.assertEqual(network._backend_endpoint(), ('api.example.test', 443))


class ApiHealthStampTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flag = Path(self.tmp.name) / 'run' / 'jam' / 'api_last_ok'
        self._patch = mock.patch.object(network, 'API_LAST_OK_FLAG', self.flag)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_no_stamp_means_no_evidence(self):
        self.assertIsNone(network.api_recently_ok())

    def test_stamp_then_fresh(self):
        network.stamp_api_ok()
        self.assertTrue(self.flag.exists(), 'stamp_api_ok must create the flag (mkdir parents + touch)')
        self.assertIs(network.api_recently_ok(), True)

    def test_stale_stamp_is_false_not_none(self):
        network.stamp_api_ok()
        old = time.time() - network.API_HEALTH_MAX_AGE_SECONDS - 60
        os.utime(self.flag, (old, old))
        self.assertIs(network.api_recently_ok(), False)

    def test_max_age_is_three_heartbeats(self):
        self.assertEqual(network.API_HEALTH_MAX_AGE_SECONDS, 15 * 60)

    def test_stamp_is_best_effort_and_never_raises(self):
        with mock.patch.object(network, 'touch_volatile_flag', side_effect=None, return_value=False):
            network.stamp_api_ok()  # no raise


if __name__ == '__main__':
    unittest.main()
