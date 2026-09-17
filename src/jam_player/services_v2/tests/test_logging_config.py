"""
setup_service_logging (common/logging_config.py): the card/backend split
from docs/LOGGING.md, the service -> enum mapping, idempotency, and the
journald priority prefix (common/journal.py) without which the drop-in's
MaxLevelStore=warning would discard the WARNING lines too.

Backend sends are patched for every test; no network, no credential files.
"""
import io
import logging
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import log_shipper, logging_config  # noqa: E402
from common.api import SystemService  # noqa: E402
from common.journal import (  # noqa: E402
    JournalPriorityFormatter,
    journal_prefix,
    stderr_is_journal,
    syslog_priority,
)
from common.log_shipper import BackendLogHandler, ShipperInternalFilter  # noqa: E402
from common.logging_config import (  # noqa: E402
    LOCAL_STREAM_LEVEL,
    SERVICE_TO_SYSTEM_SERVICE,
    log_service_start,
    service_enum_for,
    setup_service_logging,
)

# The table in docs/LOGGING.md ("Device implementation"). Binding.
CONTRACT_MAPPING = {
    # docs/LOGGING.md, mirrored by jam_player_system_service in jam-sphere's
    # packages/database/prisma. Every Pi service here; OTHER is the fallback.
    'jam-announce': 'JAM_ANNOUNCE',
    'jam-ble-provisioning': 'JAM_BLE_PROVISIONING',
    'jam-ble-state-manager': 'JAM_BLE_STATE_MANAGER',
    'jam-boot-check': 'JAM_BOOT_CHECK',
    'jam-first-boot': 'JAM_FIRST_BOOT',
    'jam-health-monitor': 'JAM_HEALTH_MONITOR',
    'jam-heartbeat': 'JAM_HEARTBEAT',
    'jam-player-display': 'JAM_PLAYER_DISPLAY',
    'jam-registration-poller': 'JAM_REGISTRATION_POLLER',
    'jam-tailscale': 'JAM_TAILSCALE',
    'jam-update': 'JAM_UPDATE',
    'jam-ws-commands': 'JAM_WEBSOCKET_COMMANDS',
    'jam-chrony-peering': 'JAM_CHRONY_PEERING',
    'jam-display-cache-prewarm': 'JAM_DISPLAY_CACHE_PREWARM',
    'jam-display-hotplug-monitor': 'JAM_DISPLAY_HOTPLUG_MONITOR',
    'jam-display-wait-for-hdmi': 'JAM_DISPLAY_WAIT_FOR_HDMI',
    'jam-installed-version-reporter': 'JAM_INSTALLED_VERSION_REPORTER',
    'jam-outlet-status-poller': 'JAM_OUTLET_STATUS_POLLER',
    'jam-venv-repair': 'JAM_VENV_REPAIR',
    'jam-content-manager': 'JAM_CONTENT_MANAGER',
}

LOCAL_LINE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - (?P<name>\S+) - (?P<level>[A-Z]+) - (?P<msg>.*)$')


def _response(status):
    resp = mock.Mock()
    resp.status_code = status
    return resp


class _RootIsolation(unittest.TestCase):
    """Empty root logger per test; backend calls captured; env controlled."""

    def setUp(self):
        self.root = logging.getLogger()
        self._saved_handlers = list(self.root.handlers)
        self._saved_level = self.root.level
        for handler in self._saved_handlers:
            self.root.removeHandler(handler)

        self.calls = []

        def api_request(**kwargs):
            self.calls.append(kwargs)
            return _response(200)

        self._patches = [
            mock.patch.object(log_shipper, 'api_request', side_effect=api_request),
            mock.patch.object(log_shipper, 'is_device_announced', return_value=True),
            mock.patch.object(log_shipper, 'get_device_uuid', return_value='device-uuid'),
            mock.patch.object(log_shipper, 'get_api_signing_private_key', return_value='key'),
            mock.patch.dict(os.environ),
        ]
        for p in self._patches:
            p.start()
        os.environ.pop('JOURNAL_STREAM', None)
        os.environ.pop(logging_config.SHIPPING_DISABLED_ENV, None)  # run_on_device.sh sets it; these tests want the shipper

    def tearDown(self):
        for handler in list(self.root.handlers):
            self.root.removeHandler(handler)
            if isinstance(handler, BackendLogHandler):
                handler.close()
        for handler in self._saved_handlers:
            self.root.addHandler(handler)
        self.root.setLevel(self._saved_level)
        for p in reversed(self._patches):
            p.stop()

    def stream_handler(self):
        found = [h for h in self.root.handlers if isinstance(h, logging.StreamHandler)]
        self.assertEqual(len(found), 1, self.root.handlers)
        return found[0]

    def backend_handler(self):
        found = [h for h in self.root.handlers if isinstance(h, BackendLogHandler)]
        self.assertEqual(len(found), 1, self.root.handlers)
        return found[0]

    def capture_card(self):
        """Redirect the local handler to a buffer; returns it."""
        buffer = io.StringIO()
        self.stream_handler().setStream(buffer)
        return buffer

    def shipped(self):
        """Flush the backend handler synchronously; return (level, message) pairs sent."""
        self.assertTrue(self.backend_handler().flush_now())
        return [(e['level'], e['message']) for call in self.calls for e in call['body']['logs']]


class ServiceMappingTests(unittest.TestCase):
    def test_mapping_matches_the_contract_table(self):
        self.assertEqual(SERVICE_TO_SYSTEM_SERVICE, CONTRACT_MAPPING)
        for name, expected in CONTRACT_MAPPING.items():
            self.assertEqual(service_enum_for(name), expected)

    def test_unknown_service_maps_to_other(self):
        for name in ('jam-heartbeat.service', '', 'nope'):
            self.assertEqual(service_enum_for(name), SystemService.OTHER)
            self.assertEqual(service_enum_for(name), 'OTHER')

    def test_main_module_retags_a_handler_created_by_an_imported_module(self):
        """jam_display_cache_prewarm imports jam_player_display, whose own
        setup_service_logging runs first; the process must still ship as
        JAM_DISPLAY_CACHE_PREWARM."""
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        # Simulate the imported module's call (not from __main__).
        logging_config.setup_service_logging('jam-player-display')
        handler = next(h for h in root.handlers if isinstance(h, BackendLogHandler))
        self.assertEqual(handler.service, SystemService.JAM_PLAYER_DISPLAY)
        # Simulate the process's own call, made from __main__.
        with mock.patch.object(logging_config, '_called_from_main_module', return_value=True):
            logging_config.setup_service_logging('jam-display-cache-prewarm')
        self.assertIs(next(h for h in root.handlers if isinstance(h, BackendLogHandler)), handler)
        self.assertEqual(handler.service, SystemService.JAM_DISPLAY_CACHE_PREWARM)
        # A later NON-main call (lazy import inside a function) must not steal it.
        with mock.patch.object(logging_config, '_called_from_main_module', return_value=False):
            logging_config.setup_service_logging('jam-player-display')
        self.assertEqual(handler.service, SystemService.JAM_DISPLAY_CACHE_PREWARM)
        for h in list(root.handlers):
            root.removeHandler(h)

    def test_every_pi_service_has_its_own_value(self):
        """No JAM service may land as OTHER or as a system-daemon value."""
        expected = {
            'jam-display-cache-prewarm': SystemService.JAM_DISPLAY_CACHE_PREWARM,
            'jam-display-hotplug-monitor': SystemService.JAM_DISPLAY_HOTPLUG_MONITOR,
            'jam-display-wait-for-hdmi': SystemService.JAM_DISPLAY_WAIT_FOR_HDMI,
            'jam-installed-version-reporter': SystemService.JAM_INSTALLED_VERSION_REPORTER,
            'jam-outlet-status-poller': SystemService.JAM_OUTLET_STATUS_POLLER,
            'jam-venv-repair': SystemService.JAM_VENV_REPAIR,
            'jam-tailscale': SystemService.JAM_TAILSCALE,
            'jam-chrony-peering': SystemService.JAM_CHRONY_PEERING,
        }
        for name, value in expected.items():
            self.assertEqual(logging_config.service_enum_for(name), value, name)
        daemons = {SystemService.NETWORK_MANAGER, SystemService.BLUETOOTH, SystemService.CHRONY, SystemService.TAILSCALE}
        for name in logging_config.SERVICE_TO_SYSTEM_SERVICE:
            self.assertNotIn(logging_config.service_enum_for(name), daemons | {SystemService.OTHER}, name)

    def test_every_service_on_disk_is_mapped(self):
        """A new jam_*.py that calls setup_service_logging must get a mapping."""
        import glob, re, os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        names = set()
        candidates = glob.glob(os.path.join(here, 'jam_*.py'))
        # jam-content-manager.service runs scenes_manager_service.py, which
        # lives one level up under src/jam_player/ and is easy to forget.
        candidates.append(os.path.join(os.path.dirname(here), 'scenes_manager_service.py'))
        for path in candidates:
            if not os.path.exists(path):
                continue
            with open(path) as f:
                names.update(re.findall(r"setup_service_logging\([\"']([^\"']+)[\"']", f.read()))
        self.assertTrue(names)
        unmapped = sorted(n for n in names if n not in logging_config.SERVICE_TO_SYSTEM_SERVICE)
        self.assertEqual(unmapped, [], f'services without a SystemService mapping: {unmapped}')

    def test_every_mapped_value_is_a_backend_enum_member(self):
        for value in SERVICE_TO_SYSTEM_SERVICE.values():
            self.assertEqual(getattr(SystemService, value), value)


class HandlerWiringTests(_RootIsolation):
    def test_installs_one_card_handler_and_one_backend_handler(self):
        setup_service_logging('jam-heartbeat', level=logging.INFO)
        self.assertEqual(len(self.root.handlers), 2)
        self.assertEqual(self.root.level, logging.INFO)

        stream = self.stream_handler()
        self.assertEqual(stream.level, logging.WARNING)
        self.assertEqual(LOCAL_STREAM_LEVEL, logging.WARNING)
        self.assertIsInstance(stream.formatter, JournalPriorityFormatter)
        self.assertTrue(any(isinstance(f, ShipperInternalFilter) for f in stream.filters))
        self.assertIs(stream.stream, sys.stderr)

        backend = self.backend_handler()
        self.assertEqual(backend.level, logging.INFO)
        self.assertEqual(backend.service, 'JAM_HEARTBEAT')

    def test_unknown_service_ships_as_other(self):
        setup_service_logging('jam-display-hotplug-monitor', level=logging.INFO)
        self.assertEqual(self.backend_handler().service, 'OTHER')

    def test_configured_level_is_the_shipped_level_not_the_card_level(self):
        setup_service_logging('jam-heartbeat', level=logging.DEBUG)
        self.assertEqual(self.root.level, logging.DEBUG)
        self.assertEqual(self.backend_handler().level, logging.DEBUG)
        self.assertEqual(self.stream_handler().level, logging.WARNING)

    def test_returns_the_service_logger(self):
        logger = setup_service_logging('jam-heartbeat', level=logging.INFO)
        self.assertEqual(logger.name, 'jam-heartbeat')
        self.assertIs(logger, logging.getLogger('jam-heartbeat'))

    def test_reads_log_level_file_when_level_not_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            level_file = Path(tmp) / 'log_level'
            with mock.patch.object(logging_config, 'LOG_LEVEL_FILE', level_file):
                setup_service_logging('jam-heartbeat')
                self.assertEqual(self.root.level, logging.INFO)  # missing file -> INFO
                level_file.write_text('debug\n')
                setup_service_logging('jam-heartbeat')
                self.assertEqual(self.root.level, logging.DEBUG)
                self.assertEqual(self.backend_handler().level, logging.DEBUG)
                level_file.write_text('verbose')
                setup_service_logging('jam-heartbeat')
                self.assertEqual(self.root.level, logging.INFO)  # garbage -> INFO


class IdempotencyTests(_RootIsolation):
    def test_second_call_does_not_duplicate_handlers(self):
        setup_service_logging('jam-heartbeat', level=logging.INFO)
        first = (self.stream_handler(), self.backend_handler())
        setup_service_logging('jam-heartbeat', level=logging.INFO)
        self.assertEqual(len(self.root.handlers), 2)
        self.assertIs(self.stream_handler(), first[0])
        self.assertIs(self.backend_handler(), first[1])

    def test_first_service_tag_wins_across_module_imports(self):
        """jam_update imports jam_player_display lazily; the process must stay JAM_UPDATE."""
        setup_service_logging('jam-update', level=logging.INFO)
        setup_service_logging('jam-player-display', level=logging.INFO)
        self.assertEqual(len(self.root.handlers), 2)
        self.assertEqual(self.backend_handler().service, 'JAM_UPDATE')

    def test_second_call_relevels_without_duplicating(self):
        setup_service_logging('jam-heartbeat', level=logging.INFO)
        setup_service_logging('jam-heartbeat', level=logging.DEBUG)
        self.assertEqual(len(self.root.handlers), 2)
        self.assertEqual(self.root.level, logging.DEBUG)
        self.assertEqual(self.backend_handler().level, logging.DEBUG)
        self.assertEqual(self.stream_handler().level, logging.WARNING)

    def test_adopts_a_pre_existing_stream_handler_and_pins_it_to_warning(self):
        foreign = logging.StreamHandler()
        foreign.setLevel(logging.DEBUG)
        self.root.addHandler(foreign)
        setup_service_logging('jam-heartbeat', level=logging.INFO)
        self.assertEqual(len(self.root.handlers), 2)
        self.assertIs(self.stream_handler(), foreign)
        self.assertEqual(foreign.level, logging.WARNING)


class RoutingTests(_RootIsolation):
    def test_info_ships_and_stays_off_the_card(self):
        logger = setup_service_logging('jam-heartbeat', level=logging.INFO)
        card = self.capture_card()
        logger.debug('debug-line')
        logger.info('info-line')
        logger.warning('warn-line')
        logger.error('error-line')

        card_lines = card.getvalue().splitlines()
        self.assertEqual([LOCAL_LINE.match(l).group('msg') for l in card_lines], ['warn-line', 'error-line'])
        self.assertEqual(self.shipped(), [('INFO', 'info-line'), ('WARNING', 'warn-line'), ('ERROR', 'error-line')])

    def test_debug_ships_when_configured_and_still_stays_off_the_card(self):
        logger = setup_service_logging('jam-heartbeat', level=logging.DEBUG)
        card = self.capture_card()
        logger.debug('debug-line')
        logger.info('info-line')
        self.assertEqual(card.getvalue(), '')
        self.assertEqual(self.shipped(), [('DEBUG', 'debug-line'), ('INFO', 'info-line')])

    def test_card_lines_keep_the_existing_format_outside_systemd(self):
        logger = setup_service_logging('jam-heartbeat', level=logging.INFO)
        card = self.capture_card()
        logger.warning('warn-line')
        match = LOCAL_LINE.match(card.getvalue().rstrip('\n'))
        self.assertIsNotNone(match, card.getvalue())
        self.assertEqual(match.group('name'), 'jam-heartbeat')
        self.assertEqual(match.group('level'), 'WARNING')

    def test_card_lines_carry_journal_priority_under_systemd(self):
        os.environ['JOURNAL_STREAM'] = '9:12345'  # what systemd sets for StandardError=journal
        logger = setup_service_logging('jam-heartbeat', level=logging.INFO)
        card = self.capture_card()
        logger.warning('warn-line')
        logger.critical('crit-line')
        try:
            raise RuntimeError('boom')
        except RuntimeError:
            logger.exception('error-line')

        lines = [l for l in card.getvalue().splitlines() if l]
        self.assertTrue(lines[0].startswith('<4>'), lines[0])
        self.assertIsNotNone(LOCAL_LINE.match(lines[0][3:]))
        self.assertTrue(lines[1].startswith('<2>'), lines[1])
        traceback_lines = lines[2:]
        self.assertGreater(len(traceback_lines), 2)
        for line in traceback_lines:  # journald parses the prefix per line
            self.assertTrue(line.startswith('<3>'), line)
        self.assertIn('RuntimeError: boom', traceback_lines[-1])

    def test_shipper_internal_records_stay_off_the_card(self):
        setup_service_logging('jam-heartbeat', level=logging.INFO)
        card = self.capture_card()
        with log_shipper._sending():
            logging.getLogger('common.api').error('Could not connect to API')
        self.assertEqual(card.getvalue(), '')
        self.assertEqual(self.backend_handler().stats()['suppressed'], 1)

    def test_service_start_banner_is_info_only(self):
        logger = setup_service_logging('jam-heartbeat', level=logging.INFO)
        card = self.capture_card()
        log_service_start(logger, 'JAM Heartbeat Service')
        self.assertEqual(card.getvalue(), '')
        self.assertEqual(self.backend_handler().stats()['queued'], 3)


class JournalTests(unittest.TestCase):
    def test_priority_mapping(self):
        self.assertEqual(syslog_priority(logging.CRITICAL), 2)
        self.assertEqual(syslog_priority(logging.ERROR), 3)
        self.assertEqual(syslog_priority(logging.WARNING), 4)
        self.assertEqual(syslog_priority(logging.INFO), 6)
        self.assertEqual(syslog_priority(logging.DEBUG), 7)
        self.assertEqual(syslog_priority(25), 6)  # rounds down, never promotes

    def test_prefix_is_applied_per_line_and_skips_empty_lines(self):
        self.assertEqual(journal_prefix('a\nb\n\nc', logging.ERROR), '<3>a\n<3>b\n\n<3>c')

    def test_formatter_auto_detects_systemd(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('JOURNAL_STREAM', None)
            self.assertFalse(stderr_is_journal())
            self.assertFalse(JournalPriorityFormatter('%(message)s').journal)
            # systemd's rule: the variable is set AND names stderr's own dev:inode.
            st = os.fstat(2)
            os.environ['JOURNAL_STREAM'] = f'{st.st_dev}:{st.st_ino}'
            self.assertTrue(stderr_is_journal())
            self.assertTrue(JournalPriorityFormatter('%(message)s').journal)
            os.environ['JOURNAL_STREAM'] = '9:1'   # some other stream: not ours
            self.assertFalse(stderr_is_journal())

    def test_formatter_output(self):
        record = logging.LogRecord('n', logging.WARNING, __file__, 1, 'hello', None, None)
        self.assertEqual(JournalPriorityFormatter('%(message)s', journal=False).format(record), 'hello')
        self.assertEqual(JournalPriorityFormatter('%(message)s', journal=True).format(record), '<4>hello')


if __name__ == '__main__':
    unittest.main()
