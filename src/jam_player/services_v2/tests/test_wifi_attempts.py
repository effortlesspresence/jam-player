"""
One WiFi connect at a time, and only the newest may report
(common.wifi_attempts), on a laptop or a player.

The first class replays the two races this bookkeeping exists to close, the
way jam_ble_provisioning drives it: begin() on each credentials write,
turn() around the connect, report() for every status.
"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.wifi_attempts import WifiAttemptLedger  # noqa: E402

WAIT = 5  # seconds; generous, only reached when a test is about to fail


class Player:
    """A stand-in for the status characteristic plus the connect thread body."""

    def __init__(self):
        self.ledger = WifiAttemptLedger()
        self.statuses = []     # what the app would see, in order
        self.connects = []     # which attempts actually ran their connect

    def write_credentials(self):
        return self.ledger.begin(lambda: self.statuses.append('connecting'))

    def report(self, attempt, status):
        return self.ledger.report(attempt, lambda: self.statuses.append(status))


class TheRacesTests(unittest.TestCase):

    def test_a_late_failed_does_not_land_on_a_newer_attempt(self):
        # no_internet, then the user retries inside the 3 s before the
        # trailing `failed`. The old attempt's `failed` must be dropped.
        player = Player()
        first = player.write_credentials()
        self.assertTrue(player.report(first, 'no_internet'))
        second = player.write_credentials()                 # the retry
        self.assertFalse(player.report(first, 'failed'), 'the superseded attempt is silenced')
        self.assertTrue(player.report(second, 'connected'))
        self.assertEqual(player.statuses, ['connecting', 'no_internet', 'connecting', 'connected'])

    def test_with_no_retry_the_trailing_failed_is_still_sent(self):
        # App builds that do not know `no_internet` rely on it.
        player = Player()
        first = player.write_credentials()
        player.report(first, 'no_internet')
        self.assertTrue(player.report(first, 'failed'))
        self.assertEqual(player.statuses, ['connecting', 'no_internet', 'failed'])

    def test_a_late_failed_just_before_the_retry_is_followed_by_connecting(self):
        # The other order: the trailing `failed` gets out first. That is
        # allowed -- it lands BEFORE the retry's `connecting`, where the apps
        # ignore it -- and it can never come after.
        player = Player()
        first = player.write_credentials()
        player.report(first, 'no_internet')
        player.report(first, 'failed')
        player.write_credentials()
        self.assertEqual(player.statuses[-2:], ['failed', 'connecting'])

    def test_cancel_then_pick_another_network_runs_one_connect_at_a_time(self):
        # Attempt 1 is inside nmcli when the user cancels and starts attempt
        # 2. Attempt 2 must wait its turn, and attempt 1 must say nothing.
        player = Player()
        in_nmcli = threading.Event()
        nmcli_may_finish = threading.Event()
        running = []
        overlap = []

        def connect_thread(attempt, block):
            try:
                with player.ledger.turn(attempt) as still_wanted:
                    if not still_wanted:
                        return
                    running.append(attempt)
                    overlap.append(len(running))
                    player.connects.append(attempt)
                    if block:
                        in_nmcli.set()
                        nmcli_may_finish.wait(WAIT)
                    running.remove(attempt)
                    if not player.ledger.is_current(attempt):
                        return                         # superseded while connecting
                    player.report(attempt, 'connected')
            finally:
                player.ledger.end()

        first = player.write_credentials()
        t1 = threading.Thread(target=connect_thread, args=(first, True))
        t1.start()
        self.assertTrue(in_nmcli.wait(WAIT))

        second = player.write_credentials()            # user cancelled, picked another
        t2 = threading.Thread(target=connect_thread, args=(second, False))
        t2.start()
        t2.join(0.2)
        self.assertTrue(t2.is_alive(), 'the second attempt waits for the first to leave nmcli')
        self.assertEqual(player.connects, [first])

        nmcli_may_finish.set()
        t1.join(WAIT); t2.join(WAIT)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(player.connects, [first, second], 'in order, never together')
        self.assertEqual(max(overlap), 1)
        self.assertEqual(player.statuses, ['connecting', 'connecting', 'connected'],
                         'only the second attempt reported a result')

    def test_an_attempt_replaced_while_it_waited_never_connects(self):
        player = Player()
        holding = threading.Event()
        release = threading.Event()

        def holder(attempt):
            with player.ledger.turn(attempt):
                holding.set()
                release.wait(WAIT)

        first = player.write_credentials()
        t1 = threading.Thread(target=holder, args=(first,))
        t1.start()
        self.assertTrue(holding.wait(WAIT))

        second = player.write_credentials()
        verdicts = {}

        def waiter(attempt):
            with player.ledger.turn(attempt) as still_wanted:
                verdicts[attempt] = still_wanted

        t2 = threading.Thread(target=waiter, args=(second,))
        t2.start()
        third = player.write_credentials()             # replaces the second while it waits
        t3 = threading.Thread(target=waiter, args=(third,))
        t3.start()
        release.set()
        for t in (t1, t2, t3):
            t.join(WAIT)
        self.assertEqual(verdicts, {second: False, third: True})


class LedgerTests(unittest.TestCase):

    def test_attempts_are_numbered_in_order_and_only_the_last_is_current(self):
        ledger = WifiAttemptLedger()
        first, second = ledger.begin(), ledger.begin()
        self.assertLess(first, second)
        self.assertFalse(ledger.is_current(first))
        self.assertTrue(ledger.is_current(second))

    def test_the_first_status_is_announced_when_the_attempt_begins(self):
        ledger = WifiAttemptLedger()
        seen = []
        attempt = ledger.begin(lambda: seen.append('connecting'))
        self.assertEqual(seen, ['connecting'])
        self.assertTrue(ledger.is_current(attempt))

    def test_end_says_when_the_last_attempt_is_done(self):
        ledger = WifiAttemptLedger()
        ledger.begin(); ledger.begin()
        self.assertFalse(ledger.end(), 'one attempt is still in flight')
        self.assertTrue(ledger.end(), 'now the session marker may come down')
        self.assertTrue(ledger.end(), 'an extra end() never goes negative')
        ledger.begin()
        self.assertTrue(ledger.end())

    def test_an_announce_that_raises_supersedes_but_is_not_counted_active(self):
        ledger = WifiAttemptLedger()
        earlier = ledger.begin()

        def broken():
            raise RuntimeError('D-Bus is gone')

        with self.assertRaises(RuntimeError):
            ledger.begin(broken)
        self.assertFalse(ledger.is_current(earlier))
        self.assertTrue(ledger.end(), 'only the earlier attempt was ever active')

    def test_a_report_never_follows_a_newer_attempts_first_status(self):
        # Many attempts begin while older ones are still trying to report.
        # Whatever the interleaving, no report from attempt N may appear in
        # the log after the announcement of an attempt newer than N.
        ledger = WifiAttemptLedger()
        log = []
        start = threading.Barrier(9)

        def one_attempt():
            start.wait(WAIT)
            for _ in range(200):
                box = {}
                box['n'] = ledger.begin(lambda: log.append(('begin', None)))
                for _ in range(3):
                    ledger.report(box['n'], lambda: log.append(('report', box['n'])))
                ledger.end()

        threads = [threading.Thread(target=one_attempt) for _ in range(8)]
        for t in threads:
            t.start()
        start.wait(WAIT)
        for t in threads:
            t.join(WAIT * 4)
        self.assertFalse(any(t.is_alive() for t in threads))

        # begin() numbers attempts 1, 2, 3... in the order their announcements
        # were logged, so the count of 'begin' entries so far IS the newest
        # attempt's number at that point in the log.
        newest = 0
        reports = 0
        for kind, attempt in log:
            if kind == 'begin':
                newest += 1
            else:
                reports += 1
                self.assertEqual(attempt, newest, 'a superseded attempt reported')
        self.assertEqual(newest, 8 * 200)
        self.assertGreater(reports, 0)


if __name__ == '__main__':
    unittest.main()
