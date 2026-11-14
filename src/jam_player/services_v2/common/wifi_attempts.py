"""
One WiFi connect at a time, and only the newest may report. Stdlib-only
bookkeeping shared by jam_ble_provisioning.py (the caller) and its tests.

THE PROBLEM. The setup app starts a connect by writing credentials over BLE,
then watches ONE status value for the result. Each write runs its connect on
a thread of its own, and nothing tied a status report to the attempt that
produced it. Two ways that went wrong:

  - After `no_internet` or `backend_unreachable` the thread waits 3 s and then
    also reports `failed`, so app builds that do not know those two codes
    still stop. If the user had started another attempt inside those 3 s, the
    late `failed` landed on top of the new attempt's `connecting`. The app
    took it as the new attempt's answer and showed a failure while the player
    was still connecting -- and then ignored the real result when it came.

  - A user who cancels in the app and picks another network starts a second
    thread while the first is still inside nmcli. Two activations ran at
    once: whichever finished last decided which network the player ended up
    on, the first one's result was reported as the second's, and "forget the
    active profile" could delete the wrong one.

THE RULES this class enforces:

  1. Every credentials write begins a new attempt, and that supersedes every
     earlier one, atomically with the new attempt's first status.
  2. A superseded attempt never reports again. The check and the report are
     one step under the same lock that begins an attempt, so a late report can
     only ever land BEFORE the new attempt's first status, never after it.
  3. Attempts take turns. One that was superseded while it waited does not
     run at all, and one that was superseded while it ran stops before it
     judges the network, forgets a profile or announces: the user has moved
     on, and the newer attempt decides where the player ends up.

The trailing `failed` is deliberately reported OUTSIDE an attempt's turn, so
the next attempt never waits out that pause.
"""
import contextlib
import threading
from typing import Callable, Iterator, Optional


class WifiAttemptLedger:
    """Numbers the app's connect attempts and says which one is current."""

    def __init__(self) -> None:
        # Guards _latest and _active, and is held across every status report.
        self._state_lock = threading.Lock()
        # Held while an attempt is connecting and judging its network.
        self._turn_lock = threading.Lock()
        self._latest = 0
        self._active = 0

    def begin(self, announce: Optional[Callable[[], None]] = None) -> int:
        """
        Start a new attempt and return its number. Every earlier attempt is
        superseded from this moment.

        `announce` reports the new attempt's first status (`connecting`). It
        runs under the same lock as report(), which is what makes rule 2 hold:
        an older attempt's report is either already out, or will be dropped.
        If it raises, the attempt is not counted as active and the error is
        passed on.
        """
        with self._state_lock:
            self._latest += 1
            attempt = self._latest
            if announce is not None:
                announce()
            self._active += 1
            return attempt

    def is_current(self, attempt: int) -> bool:
        """Is this still the newest attempt?"""
        with self._state_lock:
            return attempt == self._latest

    def report(self, attempt: int, emit: Callable[[], None]) -> bool:
        """
        Run `emit` (a status report) only if `attempt` is still the newest.
        Returns whether it ran.
        """
        with self._state_lock:
            if attempt != self._latest:
                return False
            emit()
            return True

    @contextlib.contextmanager
    def turn(self, attempt: int) -> Iterator[bool]:
        """
        Wait for any attempt already connecting to finish, then yield whether
        this one should still run (False when a newer attempt has begun while
        it waited). The turn ends when the block exits.
        """
        with self._turn_lock:
            yield self.is_current(attempt)

    def end(self) -> bool:
        """
        One attempt's thread is done. Returns True when no attempt is left,
        which is when the caller may drop its "a setup session is in flight"
        marker.
        """
        with self._state_lock:
            self._active = max(0, self._active - 1)
            return self._active == 0
