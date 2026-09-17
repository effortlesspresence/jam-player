"""
JAM Player 2.0 - Backend log shipper

The logging.Handler that carries a service's log lines off the SD card to
POST /jam-players/logs. It is the "backend" half of the rule in
docs/LOGGING.md; the "card" half is the WARNING-only stream handler that
common/logging_config.py installs next to it.

Everything here follows from three constraints: never write a log line to
the card, never block or destabilize the service that is logging, and
never keep anything on disk.

- emit() turns the record into the wire entry (service enum, level,
  message + traceback, logger name, ISO-8601 UTC device timestamp) and
  appends it to an in-memory ring buffer (deque, oldest dropped at
  capacity). It never blocks, never raises, never logs.
- One daemon thread flushes. It wakes every flush interval or as soon as
  the buffer reaches the batch threshold, takes up to 200 entries (and at
  most MAX_BATCH_BYTES serialized, under the contract's 256 KB body cap)
  and sends them as one signed POST via common.api.api_request. A send
  that fails for a reason that can pass later (exception, None response,
  429/408, any 5xx) puts the batch BACK at the head of the ring for the
  next wake; only a send the backend rejects outright (any other 4xx) drops
  it. Nothing is ever queued on disk: while the backend is unreachable the
  ring simply fills to its capacity and evicts the oldest lines. A failed
  send also ends that wake's drain, so an unreachable backend costs one
  timeout per interval rather than one per batch. (Until 2026-09-17 every
  failure dropped the batch, so a single slow Lambda cold start silently
  lost 30 s of a healthy player's lines.)
- Nothing is sent until the device is announced and has its signing
  identity (device UUID + Ed25519 key). Until then entries are dropped.
- Anything logged *by* the send path is suppressed for the duration of the
  send. common.api logs an ERROR on every timeout or connection failure
  and common.credentials warns on odd credential files; without the
  suppression an offline player would write an ERROR line to the card
  every flush interval -- from the mechanism that exists to stop card
  writes -- and feed the same line into the next batch. The suppression is
  a thread-local flag that this handler checks in emit() and that the
  local stream handler checks through ShipperInternalFilter.
- The shipper's own health is reported out of band: counters in stats()
  and at most one line per hour written directly to stderr at journal
  priority warning. Set JAM_LOG_SHIPPER_TRACE=1 in the unit's environment
  to get one such line per flush attempt while debugging.
- flush() (which logging.shutdown() calls at exit) is deliberately a no-op:
  a send there would add its full timeout, plus unbounded DNS resolution, to
  every service's exit path, on the very networks this system exists for.
  The last <=30 s of INFO lines at exit are the accepted loss (docs/
  LOGGING.md). flush_now() is the explicit synchronous send for callers
  that want one.
"""

import collections
import contextlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .credentials import (
    get_api_signing_private_key,
    get_device_uuid,
    is_device_announced,
)
from .journal import journal_prefix, stderr_is_journal

# Device endpoint from docs/LOGGING.md ("Wire contract"). Signed like every
# other /jam-players/* call.
LOGS_PATH = '/jam-players/logs'

DEFAULT_FLUSH_INTERVAL = 30.0   # seconds between timed flushes
DEFAULT_BATCH_THRESHOLD = 200   # queued entries that trigger an immediate flush
DEFAULT_RING_CAPACITY = 1000    # entries held in memory; oldest dropped beyond this
DEFAULT_REQUEST_TIMEOUT = 20.0  # a cold-started VPC Lambda can take >10 s; flush interval is 30 s    # seconds per POST from the background thread
SHUTDOWN_FLUSH_TIMEOUT = 3      # seconds for the single send made by flush()

# Wire contract limits. MAX_BATCH_BYTES stays under the 256 KB body cap
# with room for HTTP framing; MAX_MESSAGE_LENGTH matches the server-side
# truncation so a batch of long tracebacks cannot blow the body cap.
MAX_BATCH_RECORDS = 200
MAX_BATCH_BYTES = 200 * 1024
MAX_MESSAGE_LENGTH = 2048
MAX_LOGGER_NAME_LENGTH = 128

# Out-of-band health reporting.
FAILURE_REPORT_INTERVAL = 3600.0
TRACE_ENV = 'JAM_LOG_SHIPPER_TRACE'
STDERR_TAG = '[log_shipper]'

# _send() outcomes. Only SEND_DROP loses records.
SEND_OK = 'ok'
SEND_RETRY = 'retry'
SEND_DROP = 'drop'
# 4xx statuses that mean "try again later", not "this batch is bad".
TRANSIENT_4XX = frozenset({408, 429})


def api_request(**kwargs):
    """Lazy proxy to common.api.api_request.

    common.api pulls in requests and pynacl; importing it lazily keeps this
    module stdlib-only at import time (it is loaded by every service's
    logging setup, before anything else). The proxy is also the patch point
    the tests use.
    """
    from .api import api_request as _api_request
    return _api_request(**kwargs)

_LEVEL_NAMES = (
    (logging.CRITICAL, 'CRITICAL'),
    (logging.ERROR, 'ERROR'),
    (logging.WARNING, 'WARNING'),
    (logging.INFO, 'INFO'),
)

# Per-thread marker for "this thread is inside the shipper's send path".
_shipper_context = threading.local()


def in_shipper_context() -> bool:
    """True on a thread that is currently inside a BackendLogHandler send."""
    return getattr(_shipper_context, 'depth', 0) > 0


@contextlib.contextmanager
def _sending() -> Iterator[None]:
    """Mark the current thread as inside the send path for the block."""
    _shipper_context.depth = getattr(_shipper_context, 'depth', 0) + 1
    try:
        yield
    finally:
        _shipper_context.depth = max(0, getattr(_shipper_context, 'depth', 1) - 1)


class ShipperInternalFilter(logging.Filter):
    """
    Drops records logged from inside the shipper's send path.

    Attach to any handler that must not echo the shipper's own HTTP
    failures (the local stream handler does). BackendLogHandler applies
    the same rule itself in emit().
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not in_shipper_context()


def level_name(levelno: int) -> str:
    """Contract level name for a logging level number (custom levels round down)."""
    for level, name in _LEVEL_NAMES:
        if levelno >= level:
            return name
    return 'DEBUG'


def iso_utc(epoch_seconds: float) -> str:
    """ISO-8601 UTC with millisecond precision and a 'Z' suffix (the contract's deviceTs)."""
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec='milliseconds')
        .replace('+00:00', 'Z')
    )


class BackendLogHandler(logging.Handler):
    """
    logging.Handler that batches records in memory and ships them to
    POST /jam-players/logs on a background thread. See the module docstring.

    Args:
        service: jam_player_system_service enum value for every record this
            handler ships (see common.api.SystemService).
        flush_interval: seconds between timed flushes.
        batch_threshold: queued entries that trigger an immediate flush
            (clamped to the ring capacity; above MAX_BATCH_RECORDS the
            drain simply sends several batches).
        ring_capacity: entries held in memory; the oldest is dropped when
            a new one arrives at capacity.
        request_timeout: seconds per POST from the background thread.
    """

    def __init__(
        self,
        service: str,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        batch_threshold: int = DEFAULT_BATCH_THRESHOLD,
        ring_capacity: int = DEFAULT_RING_CAPACITY,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        super().__init__()
        self.service = str(service)
        self.flush_interval = max(0.01, float(flush_interval))
        self.ring_capacity = max(1, int(ring_capacity))
        self.batch_threshold = max(1, min(int(batch_threshold), self.ring_capacity))
        self.request_timeout = request_timeout

        self._buffer: collections.deque = collections.deque(maxlen=self.ring_capacity)
        # RLock, not Lock: Python runs signal handlers on the main thread, and every
        # service logs inside its SIGTERM handler. If the signal lands while the main
        # thread is inside emit()'s critical section, the handler re-enters emit();
        # a plain Lock would deadlock the process until systemd's SIGKILL.
        self._lock = threading.RLock()  # guards _buffer, _stats and the failure-report bookkeeping
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._formatter = logging.Formatter()
        self._trace = os.environ.get(TRACE_ENV) == '1'

        self._failures_unreported = 0
        self._last_failure_report: Optional[float] = None  # time.monotonic()
        self._failure_window_start = time.time()

        self._stats: Dict[str, Any] = {
            'emitted': 0,             # records accepted into the ring
            'ring_dropped': 0,        # records evicted (oldest) because the ring was full
            'suppressed': 0,          # records logged from inside the send path
            'emit_errors': 0,         # records emit() could not process at all
            'batches_sent': 0,
            'records_sent': 0,
            'batches_dropped': 0,     # sends the backend rejected outright (4xx other than 408/429)
            'records_dropped': 0,
            'batches_deferred': 0,    # sends that failed transiently; the batch went back to the ring
            'no_identity_dropped': 0,  # records discarded because the device cannot sign yet
            'last_error': None,
        }

    # ------------------------------------------------------------------
    # logging.Handler interface
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        """Queue the record. Never blocks, never raises, never logs."""
        try:
            if in_shipper_context():
                with self._lock:
                    self._stats['suppressed'] += 1
                return

            entry = self._build_entry(record)

            with self._lock:
                if len(self._buffer) >= self.ring_capacity:
                    # deque(maxlen) evicts the oldest on append; count it.
                    self._stats['ring_dropped'] += 1
                self._buffer.append(entry)
                self._stats['emitted'] += 1
                over_threshold = len(self._buffer) >= self.batch_threshold

            self._ensure_thread()
            if over_threshold:
                self._wake.set()
        except Exception:
            try:
                with self._lock:
                    self._stats['emit_errors'] += 1
            except Exception:
                pass

    def flush(self) -> None:
        """
        Called by logging.shutdown() in every service's exit path.

        Deliberately NOT a network send. This runs after the display has
        already blanked on a restart and inside SIGTERM handling everywhere;
        a send would add its full timeout, and DNS resolution (unbounded by
        the requests timeout) can add tens of seconds on the very networks
        this system exists for. Losing the last <=30 s of INFO lines on exit
        is the accepted trade (docs/LOGGING.md). flush_now() remains for
        callers that explicitly want a synchronous send.
        """
        return None

    def close(self) -> None:
        """Stop the background thread. Anything still buffered is discarded."""
        try:
            self._stop.set()
            self._wake.set()
            thread = self._thread
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.0)
        except Exception:
            pass
        finally:
            super().close()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def flush_now(self, timeout: float = SHUTDOWN_FLUSH_TIMEOUT) -> bool:
        """
        Send the oldest pending batch synchronously on the calling thread.

        Returns True when the batch was accepted or nothing was pending;
        False when it could not be sent: no identity (entries discarded), a
        transient failure (the batch stays queued for the background thread),
        or a rejection by the backend (the batch is dropped).
        Entries beyond one batch stay queued for the background thread.
        """
        try:
            with _sending():
                if self._queued() == 0:
                    return True
                if not self._has_identity():
                    self._discard_all_no_identity()
                    return False
                batch = self._take_batch()
                if not batch:
                    return True
                outcome = self._send(batch, timeout)
                if outcome == SEND_RETRY:
                    self._requeue(batch)
                return outcome == SEND_OK
        except Exception:
            return False

    def stats(self) -> Dict[str, Any]:
        """Snapshot of the counters plus 'queued' and 'thread_alive' (for tests and diagnostics)."""
        with self._lock:
            snapshot = dict(self._stats)
            snapshot['queued'] = len(self._buffer)
        thread = self._thread
        snapshot['thread_alive'] = bool(thread is not None and thread.is_alive())
        return snapshot

    # ------------------------------------------------------------------
    # Record -> wire entry
    # ------------------------------------------------------------------

    def _build_entry(self, record: logging.LogRecord) -> Dict[str, Any]:
        try:
            message = record.getMessage()
        except Exception:
            # Mismatched format args: ship the raw template rather than nothing.
            message = str(record.msg)

        if record.exc_info:
            try:
                if not record.exc_text:
                    record.exc_text = self._formatter.formatException(record.exc_info)
                message = f'{message}\n{record.exc_text}' if message else record.exc_text
            except Exception:
                pass

        if record.stack_info:
            try:
                message = f'{message}\n{self._formatter.formatStack(record.stack_info)}'
            except Exception:
                pass

        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH]

        try:
            device_ts = iso_utc(record.created)
        except Exception:
            device_ts = iso_utc(time.time())

        return {
            'service': self.service,
            'level': level_name(record.levelno),
            'message': message,
            'loggerName': str(record.name or '')[:MAX_LOGGER_NAME_LENGTH],
            'deviceTs': device_ts,
        }

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        with self._thread_lock:
            if self._thread is None and not self._stop.is_set():
                thread = threading.Thread(
                    target=self._run,
                    name=f'log-shipper-{self.service}',
                    daemon=True,
                )
                self._thread = thread
                thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(self.flush_interval)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._drain(self.request_timeout)
        except BaseException:
            # Interpreter teardown can surface anything here; stay silent.
            pass

    def _drain(self, timeout: float) -> bool:
        """
        Send queued batches until the buffer is empty or one send fails.

        Runs inside the send context so that anything api.py or
        credentials.py logs on this thread is suppressed everywhere.
        """
        with _sending():
            try:
                if self._queued() == 0:
                    return True
                if not self._has_identity():
                    self._discard_all_no_identity()
                    return False
                while True:
                    batch = self._take_batch()
                    if not batch:
                        return True
                    outcome = self._send(batch, timeout)
                    if outcome == SEND_OK:
                        continue
                    if outcome == SEND_RETRY:
                        self._requeue(batch)
                    return False
            except Exception as e:
                self._record_failure(0, f'{type(e).__name__}: {e}')
                return False

    # ------------------------------------------------------------------
    # Buffer operations
    # ------------------------------------------------------------------

    def _queued(self) -> int:
        with self._lock:
            return len(self._buffer)

    def _take_batch(self) -> List[Dict[str, Any]]:
        """Pop the oldest entries, capped by MAX_BATCH_RECORDS and MAX_BATCH_BYTES."""
        batch: List[Dict[str, Any]] = []
        size = 0
        with self._lock:
            while self._buffer and len(batch) < MAX_BATCH_RECORDS:
                entry = self._buffer[0]
                entry_size = len(json.dumps(entry)) + 1
                if batch and size + entry_size > MAX_BATCH_BYTES:
                    break
                self._buffer.popleft()
                batch.append(entry)
                size += entry_size
        return batch

    def _requeue(self, batch: List[Dict[str, Any]]) -> None:
        """Put a batch that failed transiently back at the head of the ring.

        Bounded by the ring: if the batch plus what queued meanwhile exceeds
        capacity, the OLDEST entries (the head of the batch) are evicted and
        counted as ring_dropped, exactly as emit() does when the ring is full.
        """
        with self._lock:
            combined = batch + list(self._buffer)
            overflow = max(0, len(combined) - self.ring_capacity)
            if overflow:
                self._stats['ring_dropped'] += overflow
            self._buffer = collections.deque(combined[overflow:], maxlen=self.ring_capacity)

    def _record_deferral(self, record_count: int, reason: str) -> None:
        """A send failed for a reason that may pass later; the batch is kept.
        Shares the once-an-hour failure report with real drops."""
        with self._lock:
            self._stats['batches_deferred'] += 1
        self._record_failure(0, reason, kept=record_count)
        self._trace_line(f'deferred {record_count} records ({reason})')

    def _discard_all_no_identity(self) -> int:
        with self._lock:
            count = len(self._buffer)
            self._buffer.clear()
            self._stats['no_identity_dropped'] += count
        return count

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    @staticmethod
    def _has_identity() -> bool:
        """Announced and holding both halves of the signing identity."""
        try:
            return bool(is_device_announced() and get_device_uuid() and get_api_signing_private_key())
        except Exception:
            return False

    def _send(self, batch: List[Dict[str, Any]], timeout: float) -> str:
        """One POST. SEND_OK; SEND_RETRY when the failure can pass later (the
        caller keeps the batch); SEND_DROP when the backend rejected the batch
        outright (it will never succeed, so it is counted as dropped)."""
        try:
            response = api_request(
                method='POST',
                path=LOGS_PATH,
                body={'logs': batch},
                timeout=timeout,
                signed=True,
            )
        except Exception as e:
            self._record_deferral(len(batch), f'{type(e).__name__}: {e}')
            return SEND_RETRY

        if response is None:
            self._record_deferral(len(batch), 'request failed')
            return SEND_RETRY

        status = getattr(response, 'status_code', None)
        if not (isinstance(status, int) and 200 <= status < 300):
            if isinstance(status, int) and 400 <= status < 500 and status not in TRANSIENT_4XX:
                self._record_failure(len(batch), f'HTTP {status}')
                return SEND_DROP
            self._record_deferral(len(batch), f'HTTP {status}')
            return SEND_RETRY

        with self._lock:
            self._stats['batches_sent'] += 1
            self._stats['records_sent'] += len(batch)
        self._trace_line(f'sent {len(batch)} records (HTTP {status})')
        return SEND_OK

    # ------------------------------------------------------------------
    # Out-of-band health reporting
    # ------------------------------------------------------------------

    def _record_failure(self, record_count: int, reason: str, kept: int = 0) -> None:
        """Account for a failed send. record_count entries were dropped; kept
        entries are going back to the ring (they are counted as held in the
        report because the batch is out of the buffer while this runs)."""
        now = time.monotonic()
        report: Optional[str] = None
        with self._lock:
            if record_count:
                self._stats['batches_dropped'] += 1
                self._stats['records_dropped'] += record_count
            self._stats['last_error'] = reason
            self._failures_unreported += 1
            # Never report the FIRST failure: every process on an offline
            # player would write one identical line at outage onset (and
            # oneshot services would write one per run). Start the window on
            # the first failure and report only after a full interval of
            # continued failure. api.py already records the outage itself.
            if self._last_failure_report is None:
                self._last_failure_report = now
                self._failure_window_start = time.time()
            due = now - self._last_failure_report >= FAILURE_REPORT_INTERVAL
            if due:
                report = (
                    f'{self._failures_unreported} log batch send(s) failed since '
                    f'{iso_utc(self._failure_window_start)}; last: {reason}; '
                    f'{self._stats["records_dropped"]} records dropped, {len(self._buffer) + kept} held'
                )
                self._failures_unreported = 0
                self._last_failure_report = now
                self._failure_window_start = time.time()
        if report is not None:
            self._write_stderr(report)
        if record_count:
            self._trace_line(f'dropped {record_count} records ({reason})')

    def _trace_line(self, text: str) -> None:
        if self._trace:
            self._write_stderr(text)

    @staticmethod
    def _write_stderr(text: str) -> None:
        """Write one line straight to stderr at journal priority warning. Never raises."""
        try:
            line = f'{STDERR_TAG} {text}'
            if stderr_is_journal():
                line = journal_prefix(line, logging.WARNING)
            stream = sys.stderr
            if stream is not None:
                stream.write(line + '\n')
                stream.flush()
        except Exception:
            pass
