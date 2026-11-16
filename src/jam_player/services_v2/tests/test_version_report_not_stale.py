"""
The backend must end up knowing the version a player really runs.

Found on the bench on 2026-11-16, first setup of a 7440d2d card that updated
itself to 65ed8c6 while the phone was still connected:

    12:36:46  the (old) version reporter starts, reads 7440d2d, is refused
              (401: the player is not registered yet) and starts retrying
    ~12:39    the app registers the player; the updater finishes and reports
              65ed8c6
    12:39:22  the reporter's 5th attempt gets through -- with the 7440d2d it
              read three minutes earlier

The dashboard showed 7440d2d until the version was sent again by hand. It
would have corrected itself at the next reboot (the reporter runs every boot),
but the release pages count on this field during a rollout.

Two things now prevent it, and these tests pin both:

  1. the reporter reads the version again for every attempt;
  2. the updater restarts the reporter after every update, which stops a stale
     run still retrying and sends the version once more.

Nothing on the backend changes, and nothing is added to registration: it is
the same POST /jam-players/installed-version with the same body as before.

Known remainder, left on purpose: a player that is registered more than a
couple of minutes after its update finished is refused on every retry and
reports at its next boot (the nightly reboot) instead.
"""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_installed_version_reporter as reporter  # noqa: E402
import jam_update  # noqa: E402
from common import installed_version  # noqa: E402

SERVICES_DIR = Path(__file__).resolve().parents[1]
REPORTER_UNIT = 'jam-installed-version-reporter.service'
OLD, NEW = '7440d2d2db07d869854efef50f9d2c9fbe4cd561', '65ed8c674a36f64d673f02a16d6dc64d14365160'


class ReporterReadsTheVersionForEveryAttempt(unittest.TestCase):

    def _run(self, on_disk, accepted):
        """Run the reporter. `on_disk` is what the version file holds each time
        it is read; `accepted` is the backend's answer to each attempt. Returns
        the versions that were sent, in order."""
        sent = []

        def report(version=None, branch=None):
            sent.append(version)
            return accepted[len(sent) - 1]

        with mock.patch.object(reporter, 'read_installed_version', side_effect=on_disk), \
             mock.patch.object(reporter, 'report_installed_version_to_backend', side_effect=report), \
             mock.patch.object(reporter, 'read_update_branch', return_value='testing'), \
             mock.patch.object(reporter, 'device_is_offline', return_value=False), \
             mock.patch.object(reporter.time, 'sleep'):
            self.assertEqual(reporter.main(), 0)
        return sent

    def test_the_bench_sequence_now_sends_the_new_version(self):
        # One read when the run starts, then one per attempt. The update lands
        # between the 4th and 5th attempts.
        sent = self._run(on_disk=[OLD, OLD, OLD, OLD, OLD, NEW],
                         accepted=[False, False, False, False, True])
        self.assertEqual(sent, [OLD, OLD, OLD, OLD, NEW])
        self.assertEqual(sent[-1], NEW, 'the attempt that gets through must carry what is installed NOW')

    def test_an_update_between_two_attempts_is_picked_up_at_once(self):
        sent = self._run(on_disk=[OLD, OLD, NEW], accepted=[False, True])
        self.assertEqual(sent, [OLD, NEW])

    def test_with_no_update_it_behaves_as_before(self):
        self.assertEqual(self._run(on_disk=[NEW, NEW], accepted=[True]), [NEW])

    def test_a_version_file_that_cannot_be_read_midway_keeps_the_last_value(self):
        sent = self._run(on_disk=[OLD, OLD, None], accepted=[False, True])
        self.assertEqual(sent, [OLD, OLD])


class UpdaterRestartsTheReporter(unittest.TestCase):
    SESSION_CHECK = '<ble-session-check>'

    def _restart(self, session_active):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        calls = []

        def run(cmd, *a, **k):
            calls.append(list(cmd))
            return (True, 'active', '')

        def session():
            calls.append([self.SESSION_CHECK])
            return session_active

        with mock.patch.object(jam_update, 'FIRST_CONNECT_UPDATE_FLAG', root / 'run' / 'first_connect'), \
             mock.patch.object(jam_update, 'UPDATE_IN_PROGRESS_FLAG', root / 'run' / 'in-progress'), \
             mock.patch.object(jam_update, '_ble_session_active', side_effect=session), \
             mock.patch.object(jam_update, 'run_command', side_effect=run), \
             mock.patch.object(jam_update.time, 'sleep'):
            jam_update.restart_services(bluetooth_conf_changed=False)
        return calls

    def test_every_update_restarts_the_reporter_without_waiting_for_it(self):
        for session_active in (True, False):
            calls = self._restart(session_active)
            self.assertIn(['systemctl', 'restart', '--no-block', REPORTER_UNIT], calls,
                          f'session_active={session_active}')

    def test_a_phone_mid_setup_does_not_hold_it_back(self):
        # The bench case: a setup session was live, so the BLE units stayed on
        # old code. The reporter is not a BLE unit and must not wait for that.
        calls = self._restart(session_active=True)
        reporter_at = calls.index(['systemctl', 'restart', '--no-block', REPORTER_UNIT])
        self.assertLess(reporter_at, calls.index([self.SESSION_CHECK]))

    def test_it_is_restarted_exactly_once(self):
        calls = self._restart(session_active=False)
        self.assertEqual(sum(1 for c in calls if c[-1] == REPORTER_UNIT and c[1] == 'restart'), 1)

    def test_the_unit_waits_for_the_updater_to_finish(self):
        # That ordering is what makes the fresh run read the NEW version: it
        # cannot start until jam-update has exited.
        unit = (SERVICES_DIR.parents[2] / 'systemd' / REPORTER_UNIT).read_text()
        self.assertRegex(unit, r'(?m)^After=jam-update\.service$')


class NothingChangesOnTheWire(unittest.TestCase):

    def test_the_report_is_the_same_request_as_before(self):
        # Old backends, and old players talking to a new backend, are untouched:
        # same endpoint, same two fields, and no registration call carries a
        # version.
        source = inspect.getsource(installed_version.report_installed_version_to_backend)
        self.assertIn("path='/jam-players/installed-version'", source)
        self.assertIn("'installedVersion': version", source)
        self.assertIn("body['branch'] = branch", source)

    def test_the_setup_services_are_not_part_of_this_fix(self):
        # Registration is left exactly as it was: neither service that handles
        # it knows about version reporting.
        for filename in ('jam_ble_provisioning.py', 'jam_registration_poller.py'):
            source = (SERVICES_DIR / filename).read_text()
            self.assertNotIn('installed_version', source, filename)
            self.assertNotIn(REPORTER_UNIT, source, filename)


if __name__ == '__main__':
    unittest.main()
