"""
BackendLogHandler (common/log_shipper.py): the in-memory, drop-on-failure
shipper behind docs/LOGGING.md.

Nothing here touches the network or the credential files: api_request and
the identity readers are patched on the module under test for the whole
lifetime of every test (a handler's background thread can wake at any
moment). Runs on a JAM Player (tests/run_on_device.sh) because
common.log_shipper pulls in common.api, which needs the device venv
(requests, pynacl).
"""
import io
import logging
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import log_shipper  # noqa: E402
from common.log_shipper import (  # noqa: E402
    LOGS_PATH,
    MAX_BATCH_RECORDS,
    MAX_LOGGER_NAME_LENGTH,
    MAX_MESSAGE_LENGTH,
    SHUTDOWN_FLUSH_TIMEOUT,
    BackendLogHandler,
    ShipperInternalFilter,
    iso_utc,
    level_name,
)

# Upper bound for background-thread assertions; generous for a loaded Pi.
WAIT = 5.0


def _record(msg='hello', level=logging.INFO, name='jam-test', args=None, exc_info=None, created=None):
    rec = logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)
    if created is not None:
        rec.created = created
    return rec


def _response(status):
    resp = mock.Mock()
    resp.status_code = status
    return resp


class _Backend:
    """
    Stand-in for common.api.api_request plus the identity readers.

    Attributes can be changed mid-test: `status` (None -> api_request
    returns None, as it does on timeout/connection error), `exc` (raised
    from api_request), `identity`, `uuid`, `key`, `on_call` (runs inside
    the call, used to simulate api.py logging during a send).
    """

    def __init__(self):
        self.status = 200
        self.exc = None
        self.identity = True
        self.uuid = 'device-uuid'
        self.key = 'signing-key'
        self.on_call = None
        self.calls = []

    def api_request(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        if self.exc is not None:
            raise self.exc
        return None if self.status is None else _response(self.status)

    def wait_for_calls(self, count, timeout=WAIT):
        deadline = time.monotonic() + timeout
        while len(self.calls) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return len(self.calls)


class _ShipperTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()
        self._patches = [
            mock.patch.object(log_shipper, 'api_request', side_effect=self.backend.api_request),
            mock.patch.object(log_shipper, 'is_device_announced', side_effect=lambda: self.backend.identity),
            mock.patch.object(log_shipper, 'get_device_uuid', side_effect=lambda: self.backend.uuid),
            mock.patch.object(log_shipper, 'get_api_signing_private_key', side_effect=lambda: self.backend.key),
            mock.patch.dict(os.environ),
            # The shipper's out-of-band failure line goes straight to stderr;
            # capture it so failure tests can assert on it and stay quiet.
            mock.patch('sys.stderr', new_callable=io.StringIO),
        ]
        for p in self._patches:
            started = p.start()
            if isinstance(started, io.StringIO):
                self.stderr = started
        os.environ.pop('JOURNAL_STREAM', None)
        os.environ.pop(log_shipper.TRACE_ENV, None)
        os.environ.pop('JAM_LOG_SHIPPING_DISABLED', None)
        self._handlers = []

    def tearDown(self):
        # Close (and join) every handler's thread BEFORE the patches come off.
        for handler in self._handlers:
            handler.close()
        for p in reversed(self._patches):
            p.stop()

    def handler(self, **kwargs):
        """A handler whose thread stays asleep unless the test wants otherwise."""
        kwargs.setdefault('flush_interval', 3600)
        kwargs.setdefault('batch_threshold', 1000)
        kwargs.setdefault('ring_capacity', 1000)
        handler = BackendLogHandler('JAM_HEARTBEAT', **kwargs)
        self._handlers.append(handler)
        return handler

    def sent_messages(self, call_index=0):
        return [entry['message'] for entry in self.backend.calls[call_index]['body']['logs']]


class EntryFormatTests(_ShipperTestCase):
    def test_wire_entry_shape_and_iso_utc_timestamp(self):
        handler = self.handler()
        created = datetime(2026, 9, 10, 15, 4, 5, 500000, tzinfo=timezone.utc).timestamp()
        handler.emit(_record(
            'Post-boot BLE recovery window closed (15 min)',
            name='jam-ble-state-manager', created=created,
        ))
        self.assertTrue(handler.flush_now())

        call = self.backend.calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['path'], LOGS_PATH)
        self.assertEqual(call['path'], '/jam-players/logs')
        self.assertTrue(call['signed'])
        self.assertEqual(list(call['body'].keys()), ['logs'])
        self.assertEqual(call['body']['logs'], [{
            'service': 'JAM_HEARTBEAT',
            'level': 'INFO',
            'message': 'Post-boot BLE recovery window closed (15 min)',
            'loggerName': 'jam-ble-state-manager',
            'deviceTs': '2026-09-10T15:04:05.500Z',
        }])

    def test_iso_utc_is_utc_with_millis_and_z(self):
        self.assertEqual(iso_utc(0), '1970-01-01T00:00:00.000Z')
        created = datetime(2026, 1, 2, 3, 4, 5, 250000, tzinfo=timezone.utc).timestamp()
        self.assertEqual(iso_utc(created), '2026-01-02T03:04:05.250Z')

    def test_exception_text_is_appended(self):
        handler = self.handler()
        try:
            raise ValueError('boom')
        except ValueError:
            handler.emit(_record('update failed', level=logging.ERROR, exc_info=sys.exc_info()))
        handler.flush_now()
        entry = self.backend.calls[0]['body']['logs'][0]
        self.assertEqual(entry['level'], 'ERROR')
        self.assertTrue(entry['message'].startswith('update failed\nTraceback (most recent call last):'))
        self.assertIn('ValueError: boom', entry['message'])

    def test_message_truncated_to_contract_limit(self):
        handler = self.handler()
        handler.emit(_record('x' * (MAX_MESSAGE_LENGTH * 3)))
        handler.flush_now()
        self.assertEqual(len(self.sent_messages()[0]), MAX_MESSAGE_LENGTH)

    def test_logger_name_truncated_to_contract_limit(self):
        handler = self.handler()
        handler.emit(_record(name='n' * (MAX_LOGGER_NAME_LENGTH * 2)))
        handler.flush_now()
        self.assertEqual(len(self.backend.calls[0]['body']['logs'][0]['loggerName']), MAX_LOGGER_NAME_LENGTH)

    def test_level_names_follow_the_contract(self):
        self.assertEqual(level_name(logging.DEBUG), 'DEBUG')
        self.assertEqual(level_name(logging.INFO), 'INFO')
        self.assertEqual(level_name(logging.WARNING), 'WARNING')
        self.assertEqual(level_name(logging.ERROR), 'ERROR')
        self.assertEqual(level_name(logging.CRITICAL), 'CRITICAL')
        # Custom levels round DOWN so nothing is promoted.
        self.assertEqual(level_name(25), 'INFO')
        self.assertEqual(level_name(5), 'DEBUG')
        self.assertEqual(level_name(99), 'CRITICAL')


class EmitSafetyTests(_ShipperTestCase):
    def test_emit_survives_mismatched_format_args(self):
        handler = self.handler()
        handler.emit(_record('value %s and %s', args=('only-one',)))  # getMessage() raises TypeError
        self.assertEqual(handler.stats()['queued'], 1)
        handler.flush_now()
        self.assertEqual(self.sent_messages(), ['value %s and %s'])

    def test_emit_survives_internal_failure(self):
        handler = self.handler()
        with mock.patch.object(handler, '_build_entry', side_effect=RuntimeError('broken')):
            handler.emit(_record())
        self.assertEqual(handler.stats()['emit_errors'], 1)
        self.assertEqual(handler.stats()['queued'], 0)

    def test_emit_inside_send_context_is_suppressed(self):
        handler = self.handler()
        with log_shipper._sending():
            handler.emit(_record('logged by api.py during a send'))
        stats = handler.stats()
        self.assertEqual(stats['queued'], 0)
        self.assertEqual(stats['suppressed'], 1)

    def test_filter_blocks_records_only_inside_send_context(self):
        record_filter = ShipperInternalFilter()
        self.assertTrue(record_filter.filter(_record()))
        with log_shipper._sending():
            self.assertFalse(record_filter.filter(_record()))
        self.assertTrue(record_filter.filter(_record()))


class RingBufferTests(_ShipperTestCase):
    def test_drops_oldest_at_capacity(self):
        handler = self.handler(ring_capacity=5)
        with mock.patch.object(handler, '_ensure_thread'):  # no background drain in this test
            for i in range(8):
                handler.emit(_record(str(i)))
        stats = handler.stats()
        self.assertEqual(stats['queued'], 5)
        self.assertEqual(stats['ring_dropped'], 3)
        self.assertEqual(stats['emitted'], 8)
        self.assertTrue(handler.flush_now())
        self.assertEqual(self.sent_messages(), ['3', '4', '5', '6', '7'])


class BatchingTests(_ShipperTestCase):
    def test_threshold_triggers_immediate_flush(self):
        handler = self.handler(batch_threshold=5)
        for i in range(4):
            handler.emit(_record(str(i)))
        self.assertEqual(self.backend.calls, [])  # thread asleep: nothing until the threshold
        handler.emit(_record('4'))
        self.assertEqual(self.backend.wait_for_calls(1), 1)
        self.assertEqual(self.sent_messages(), ['0', '1', '2', '3', '4'])
        self.assertEqual(handler.stats()['batches_sent'], 1)
        self.assertEqual(handler.stats()['records_sent'], 5)

    def test_interval_triggers_flush(self):
        handler = self.handler(flush_interval=0.2)
        for i in range(3):
            handler.emit(_record(str(i)))
        self.assertEqual(self.backend.wait_for_calls(1), 1)
        self.assertEqual(self.sent_messages(), ['0', '1', '2'])
        self.assertEqual(handler.stats()['queued'], 0)

    def test_drain_splits_into_contract_sized_batches(self):
        handler = self.handler(batch_threshold=450)
        for i in range(450):
            handler.emit(_record(str(i)))
        self.assertEqual(self.backend.wait_for_calls(3), 3)
        sizes = [len(call['body']['logs']) for call in self.backend.calls]
        self.assertEqual(sizes, [MAX_BATCH_RECORDS, MAX_BATCH_RECORDS, 50])
        self.assertEqual(handler.stats()['queued'], 0)

    def test_batch_bytes_cap_splits_oversized_batches(self):
        handler = self.handler()
        with mock.patch.object(handler, '_ensure_thread'):
            for i in range(MAX_BATCH_RECORDS):
                handler.emit(_record('m' * MAX_MESSAGE_LENGTH))
        self.assertTrue(handler.flush_now())
        sent = len(self.backend.calls[0]['body']['logs'])
        self.assertLess(sent, MAX_BATCH_RECORDS)
        self.assertLessEqual(len(log_shipper.json.dumps(self.backend.calls[0]['body'])), 256 * 1024)
        self.assertEqual(handler.stats()['queued'], MAX_BATCH_RECORDS - sent)

    def test_background_send_uses_request_timeout(self):
        handler = self.handler(batch_threshold=1, request_timeout=7)
        handler.emit(_record())
        self.assertEqual(self.backend.wait_for_calls(1), 1)
        self.assertEqual(self.backend.calls[0]['timeout'], 7)


class FlushNowTests(_ShipperTestCase):
    def test_sends_one_batch_synchronously_with_short_timeout(self):
        handler = self.handler()
        with mock.patch.object(handler, '_ensure_thread'):
            for i in range(MAX_BATCH_RECORDS + 50):
                handler.emit(_record(str(i)))
        self.assertTrue(handler.flush_now())
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.backend.calls[0]['body']['logs']), MAX_BATCH_RECORDS)
        self.assertLessEqual(self.backend.calls[0]['timeout'], SHUTDOWN_FLUSH_TIMEOUT)
        self.assertLessEqual(SHUTDOWN_FLUSH_TIMEOUT, 3)
        self.assertEqual(handler.stats()['queued'], 50)

    def test_handler_flush_is_a_no_op_at_shutdown(self):
        """logging.shutdown() calls Handler.flush() in every exit path, after the
        display has blanked and inside SIGTERM handling; a send there would add
        its timeout plus unbounded DNS to every restart. It must not send."""
        handler = self.handler()
        handler.emit(_record())
        handler.flush()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(handler.stats()['queued'], 1)

    def test_nothing_pending_is_a_success_without_a_request(self):
        handler = self.handler()
        self.assertTrue(handler.flush_now())
        self.assertEqual(self.backend.calls, [])


class FailureTests(_ShipperTestCase):
    def _emit_and_flush(self, handler, count=3):
        for i in range(count):
            handler.emit(_record(str(i)))
        return handler.flush_now()

    def test_request_exception_keeps_the_batch_for_the_next_flush(self):
        """A slow Lambda cold start used to cost 30 s of a healthy player's lines."""
        handler = self.handler()
        self.backend.exc = RuntimeError('boom')
        self.assertFalse(self._emit_and_flush(handler))
        stats = handler.stats()
        self.assertEqual(stats['queued'], 3, 'the batch went back to the ring')
        self.assertEqual(stats['batches_deferred'], 1)
        self.assertEqual(stats['batches_dropped'], 0)
        self.assertEqual(stats['records_dropped'], 0)
        self.assertIn('RuntimeError', stats['last_error'])
        self.backend.exc = None
        self.assertTrue(handler.flush_now())
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.sent_messages(call_index=1), ['0', '1', '2'], 'same records, same order, once')
    def test_none_response_keeps_the_batch(self):
        handler = self.handler()
        self.backend.status = None  # api_request's timeout / connection-error result
        self.assertFalse(self._emit_and_flush(handler))
        self.assertEqual(handler.stats()['queued'], 3)
        self.assertEqual(handler.stats()['batches_deferred'], 1)
        self.assertEqual(handler.stats()['batches_dropped'], 0)
    def test_transient_statuses_keep_the_batch(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                handler = self.handler()
                self.backend.status = status
                self.assertFalse(self._emit_and_flush(handler))
                self.assertEqual(handler.stats()['queued'], 3)
                self.assertEqual(handler.stats()['batches_dropped'], 0)
                self.assertEqual(handler.stats()['last_error'], f'HTTP {status}')

    def test_a_rejected_batch_is_dropped_not_retried_forever(self):
        """400/401/404/413: the backend has said no; resending would never help."""
        for status in (400, 401, 403, 404, 413):
            with self.subTest(status=status):
                handler = self.handler()
                self.backend.status = status
                self.assertFalse(self._emit_and_flush(handler))
                self.assertEqual(handler.stats()['queued'], 0)
                self.assertEqual(handler.stats()['batches_dropped'], 1)
                self.assertEqual(handler.stats()['records_dropped'], 3)
                self.assertEqual(handler.stats()['last_error'], f'HTTP {status}')

    def test_a_kept_batch_still_yields_to_the_ring_capacity(self):
        """Offline for a long time: the ring, not the retry, bounds memory."""
        handler = self.handler(ring_capacity=5)
        self.backend.status = 503
        with mock.patch.object(handler, '_ensure_thread'):
            for i in range(3):
                handler.emit(_record(str(i)))
            self.assertFalse(handler.flush_now())          # 0,1,2 kept
            for i in range(3, 7):
                handler.emit(_record(str(i)))               # 7 entries for a ring of 5
        stats = handler.stats()
        self.assertEqual(stats['queued'], 5)
        self.assertEqual(stats['ring_dropped'], 2, 'the two oldest kept entries were evicted')
        self.backend.status = 200
        self.assertTrue(handler.flush_now())
        self.assertEqual(self.sent_messages(call_index=1), ['2', '3', '4', '5', '6'])
    def test_any_2xx_is_accepted(self):
        for status in (200, 202, 204):
            with self.subTest(status=status):
                handler = self.handler()
                self.backend.status = status
                self.assertTrue(self._emit_and_flush(handler))
                self.assertEqual(handler.stats()['batches_dropped'], 0)

    def test_failed_send_ends_the_drain_cycle(self):
        """One timeout per interval when the backend is down, not one per batch."""
        handler = self.handler(batch_threshold=400)
        self.backend.status = 503
        for i in range(400):
            handler.emit(_record(str(i)))
        self.assertEqual(self.backend.wait_for_calls(1), 1)
        time.sleep(0.3)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(handler.stats()['queued'], 400, 'the failed batch went back; nothing was lost')
    def test_no_send_without_announce(self):
        handler = self.handler()
        self.backend.identity = False
        self.assertFalse(self._emit_and_flush(handler))
        self.assertEqual(self.backend.calls, [])
        stats = handler.stats()
        self.assertEqual(stats['queued'], 0)
        self.assertEqual(stats['no_identity_dropped'], 3)
        self.assertEqual(stats['batches_dropped'], 0)  # expected state, not a failure

    def test_no_send_without_device_uuid(self):
        handler = self.handler()
        self.backend.uuid = None
        self.assertFalse(self._emit_and_flush(handler))
        self.assertEqual(self.backend.calls, [])

    def test_no_send_without_signing_key(self):
        handler = self.handler()
        self.backend.key = ''
        self.assertFalse(self._emit_and_flush(handler))
        self.assertEqual(self.backend.calls, [])

    def test_first_failure_writes_nothing_then_one_line_per_hour(self):
        """The onset of an outage is already recorded by common.api; the shipper
        must not add one identical line per process (or per oneshot run). It
        starts its window on the first failure and reports only after a full
        FAILURE_REPORT_INTERVAL of continued failure."""
        handler = self.handler()
        self.backend.status = 503
        for _ in range(3):
            self._emit_and_flush(handler, count=1)
        self.assertEqual(self.stderr.getvalue(), '', 'no line on the first failures')
        handler._last_failure_report -= log_shipper.FAILURE_REPORT_INTERVAL + 1  # an hour passes
        self._emit_and_flush(handler, count=1)
        lines = self.stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn('4 log batch send(s) failed since', lines[0])
        self.assertIn('HTTP 503', lines[0])
        self.assertIn('4 held', lines[0], 'the kept records are reported, not silently lost')
        self._emit_and_flush(handler, count=1)
        self.assertEqual(len(self.stderr.getvalue().splitlines()), 1, 'at most one line per interval')
        self.assertEqual(handler.stats()['batches_deferred'], 5)
        self.assertEqual(handler.stats()['batches_dropped'], 0)
    def test_stderr_report_carries_journal_priority_under_systemd(self):
        handler = self.handler()
        self.backend.status = None
        # The real check compares stderr's dev:ino with $JOURNAL_STREAM; under
        # the test runner stderr is not the journal, so assert the branch directly.
        with mock.patch.object(log_shipper, 'stderr_is_journal', return_value=True):
            self._emit_and_flush(handler, count=1)
            handler._last_failure_report -= log_shipper.FAILURE_REPORT_INTERVAL + 1
            self._emit_and_flush(handler, count=1)
        self.assertTrue(self.stderr.getvalue().startswith('<4>[log_shipper] '), self.stderr.getvalue())

    def test_success_is_silent_on_stderr(self):
        handler = self.handler()
        self.assertTrue(self._emit_and_flush(handler))
        self.assertEqual(self.stderr.getvalue(), '')

    def test_trace_env_reports_every_flush(self):
        os.environ[log_shipper.TRACE_ENV] = '1'
        handler = self.handler()
        self.assertTrue(self._emit_and_flush(handler, count=2))
        self.assertEqual(self.stderr.getvalue(), '[log_shipper] sent 2 records (HTTP 200)\n')

    def test_stderr_failure_does_not_propagate(self):
        handler = self.handler()
        self.backend.status = None
        with mock.patch('sys.stderr', new=None):
            self.assertFalse(self._emit_and_flush(handler, count=1))


class RecursionTests(_ShipperTestCase):
    def test_backend_logging_during_a_send_is_neither_shipped_nor_echoed(self):
        root = logging.getLogger()
        handler = self.handler()
        root.addHandler(handler)
        try:
            self.backend.on_call = lambda: logging.getLogger('common.api').error('Could not connect to API')
            self.backend.status = 503
            handler.emit(_record('a real line'))
            self.assertFalse(handler.flush_now())
            stats = handler.stats()
            # The 503 keeps the real line for the next flush; api.py's ERROR,
            # logged from inside the send, must NOT have joined it.
            self.assertEqual(stats['queued'], 1)
            self.assertEqual(stats['suppressed'], 1)
            self.assertEqual(len(self.backend.calls), 1)
            self.backend.status = 200
            self.assertTrue(handler.flush_now())
            self.assertEqual(self.sent_messages(call_index=1), ['a real line'])
        finally:
            root.removeHandler(handler)


class LifecycleTests(_ShipperTestCase):
    def test_thread_starts_on_first_emit_and_stops_on_close(self):
        handler = self.handler()
        self.assertFalse(handler.stats()['thread_alive'])
        handler.emit(_record())
        self.assertTrue(handler.stats()['thread_alive'])
        handler.close()
        self.assertFalse(handler.stats()['thread_alive'])
        handler.emit(_record())  # after close: still never raises, never restarts the thread
        self.assertFalse(handler.stats()['thread_alive'])

    def test_thread_is_daemon(self):
        handler = self.handler()
        handler.emit(_record())
        self.assertTrue(handler._thread.daemon)

    def test_threshold_clamped_to_capacity(self):
        handler = self.handler(batch_threshold=5000, ring_capacity=10)
        self.assertEqual(handler.batch_threshold, 10)


if __name__ == '__main__':
    unittest.main()
