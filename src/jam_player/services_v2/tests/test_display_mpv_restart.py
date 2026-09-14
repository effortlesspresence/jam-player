"""
The display service must never restart lightdm, and must never spin.

Audit #11. Two independent defects met here and produced a black screen on a
player with perfectly good cached content:

  * _start_video_playback assigned self.mpv BEFORE four early returns, so a
    missing media file left a handle with no process. The playback loop reads
    `self.mpv and not self.mpv.is_running()` as "mpv crashed", so that state
    produced phantom crashes forever.
  * Five of those inside 30 s ran `systemctl restart lightdm`. Restarting
    lightdm on this image is what CAUSES permanent black screens (a2a099d,
    7440d2d): the new X session takes DRM master from mpv, lightdm hits its
    GObject teardown crash, and on a static-image scene mpv never reclaims the
    surface. On a healthy player lightdm is expected to sit `failed`.

The loop also never pinged the systemd watchdog on that path, so the unit was
killed at WatchdogSec=60 and left `failed` after StartLimitBurst.

The source gate runs anywhere. The behavioural tests import the display module
and so run on a JAM Player.
"""
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
SERVICES_DIR = _HERE.parents[1]
for candidate in (SERVICES_DIR, _HERE.parents[2]):
    sys.path.insert(0, str(candidate))

try:
    import jam_player_display as disp
    HAVE_DISPLAY = True
    IMPORT_ERROR = ""
except Exception as exc:                                    # pragma: no cover
    disp = None
    HAVE_DISPLAY = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

DEVICE_ONLY = f"needs the on-device venv ({IMPORT_ERROR})"


def _restart_lightdm_offenders(source: str):
    """Calls that would restart lightdm, in either the argv or the shell form.

    Deliberately narrow so prose and log lines about NOT restarting lightdm do
    not trip it. `systemctl stop/start lightdm.service` is not flagged:
    jam_update does that once, on purpose, when the Restart=no drop-in changes.
    """
    tree = ast.parse(source)
    # Docstrings cannot execute anything, and the ones in this repo quote the
    # old command on purpose to explain why it must never come back.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, 'body', None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            words = [
                e.value.lower() for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if (any('lightdm' in w for w in words)
                    and any(w == 'restart' for w in words)
                    and any('systemctl' in w for w in words)):
                offenders.append((node.lineno, ' '.join(words)))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            low = node.value.lower()
            if 'systemctl restart lightdm' in low or 'restart lightdm.service' in low:
                offenders.append((node.lineno, node.value[:60]))
    return offenders


class NoLightdmRestartGate(unittest.TestCase):
    """Permanent: no JAM service may restart lightdm. Runs on a laptop too."""

    def test_no_service_restarts_lightdm(self):
        checked = 0
        for path in sorted(SERVICES_DIR.glob('*.py')):
            offenders = _restart_lightdm_offenders(path.read_text())
            checked += 1
            self.assertEqual(
                offenders, [],
                f"{path.name} restarts lightdm at line(s) "
                f"{[o[0] for o in offenders]}. Restarting lightdm on this image "
                f"causes permanent black screens -- see _note_mpv_crash in "
                f"jam_player_display.py and the header of "
                f"jam_display_hotplug_monitor.py.",
            )
        self.assertGreater(checked, 5, 'gate did not find the service modules')

    def test_the_gate_would_catch_a_reintroduction(self):
        argv_form = "import subprocess\nsubprocess.run(['systemctl', 'restart', 'lightdm'])\n"
        shell_form = "import os\nos.system('systemctl restart lightdm')\n"
        prose = (
            "def f():\n"
            "    '''We must NOT restart lightdm here.'''\n"
            "    log('NOT restarting lightdm (it causes black screens)')\n"
        )
        stop_start = "run_command(['systemctl', 'stop', 'lightdm.service'])\n"
        explaining_docstring = (
            "def f():\n"
            "    '''This used to run `systemctl restart lightdm`; never again.'''\n"
            "    return 1\n"
        )
        self.assertTrue(_restart_lightdm_offenders(argv_form))
        self.assertTrue(_restart_lightdm_offenders(shell_form))
        self.assertEqual(_restart_lightdm_offenders(prose), [])
        self.assertEqual(_restart_lightdm_offenders(stop_start), [])
        self.assertEqual(_restart_lightdm_offenders(explaining_docstring), [],
                         'a docstring quoting the command is documentation, not a call')


def _bare_manager():
    """A manager with only the attributes these tests touch, built without
    __init__ so no signal handlers or hardware probing run."""
    mgr = disp.JamPlayerDisplayManager.__new__(disp.JamPlayerDisplayManager)
    mgr.running = True
    mgr.mpv = None
    mgr.is_playing = False
    mgr._mpv_crash_times = []
    mgr._mpv_crash_threshold = 5
    mgr._mpv_crash_window_seconds = 30
    mgr._mpv_restart_backoff_sec = disp.MPV_RESTART_BACKOFF_MIN_SEC
    mgr._display_trouble_log_sec = {}
    return mgr


@unittest.skipUnless(HAVE_DISPLAY, DEVICE_ONLY)
class StartVideoPlaybackTests(unittest.TestCase):
    """self.mpv is assigned ONLY when a process is really running, and the
    caller can tell "scheduled off" apart from "cannot start"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name)
        self.mgr = _bare_manager()
        self._p = [
            mock.patch.object(disp, 'kill_feh_processes'),
            mock.patch.object(disp, 'get_rotation_angle', return_value=0),
            mock.patch.object(disp.constants, 'APP_DATA_LIVE_MEDIA_DIR', str(self.media)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def _client(self, started=True, alive=None):
        client = mock.MagicMock()
        client.start_mpv.return_value = started
        client.is_running.return_value = started if alive is None else alive
        return client

    def _run(self, scenes, started=True, alive=None):
        client = self._client(started, alive)
        with mock.patch.object(self.mgr, '_load_scenes', return_value=scenes), \
             mock.patch.object(disp, 'MpvIpcClient', return_value=client):
            outcome = self.mgr._start_video_playback()
        return outcome, client

    def _write(self, name):
        (self.media / name).write_bytes(b'x')
        return name

    def test_nothing_scheduled_is_not_a_failure(self):
        """The ordinary state of a venue outside opening hours. The caller
        routes this to the branded wait screen, never to a retry path."""
        outcome, _ = self._run([])
        self.assertEqual(outcome, disp.PLAYBACK_NOTHING_SCHEDULED)
        self.assertIsNone(self.mgr.mpv)

    def test_scene_without_a_media_file_is_no_playable_file(self):
        outcome, _ = self._run([{'id': 'a'}])
        self.assertEqual(outcome, disp.PLAYBACK_NO_PLAYABLE_FILE)
        self.assertIsNone(self.mgr.mpv)

    def test_every_file_missing_is_no_playable_file(self):
        outcome, client = self._run([{'id': 'a', 'media_file': 'gone.mp4'}])
        self.assertEqual(outcome, disp.PLAYBACK_NO_PLAYABLE_FILE)
        self.assertIsNone(self.mgr.mpv)
        client.start_mpv.assert_not_called()

    def test_a_dead_mpv_is_killed_not_orphaned(self):
        """start_mpv failed and the process is gone: clean up, keep no handle."""
        self._write('ok.mp4')
        outcome, client = self._run([{'id': 'a', 'media_file': 'ok.mp4'}], started=False, alive=False)
        self.assertEqual(outcome, disp.PLAYBACK_MPV_FAILED)
        self.assertIsNone(self.mgr.mpv)
        client.stop_mpv.assert_called_once()

    def test_a_slow_socket_adopts_the_running_process(self):
        """start_mpv only waits 5 s for its IPC socket. On a slow cold boot the
        process is alive and the socket is moments away. Dropping it would
        orphan a fullscreen mpv nothing can kill, and the next attempt would
        unlink its socket and spawn a second one to fight it for the screen."""
        self._write('ok.mp4')
        outcome, client = self._run([{'id': 'a', 'media_file': 'ok.mp4'}], started=False, alive=True)
        self.assertEqual(outcome, disp.PLAYBACK_STARTED)
        self.assertIs(self.mgr.mpv, client)
        client.stop_mpv.assert_not_called()

    def test_starts_on_the_first_scene_whose_file_exists(self):
        self._write('second.mp4')
        outcome, client = self._run([
            {'id': 'a', 'media_file': 'missing.mp4'},
            {'id': 'b', 'media_file': 'second.mp4'},
        ])
        self.assertEqual(outcome, disp.PLAYBACK_STARTED)
        self.assertIs(self.mgr.mpv, client)
        self.assertTrue(self.mgr.is_playing)
        self.assertEqual(
            client.start_mpv.call_args.kwargs['initial_file'],
            str(self.media / 'second.mp4'),
        )

    def test_loop_follows_the_playable_count_not_the_scheduled_count(self):
        """Three scheduled, one playable: the wall clock cannot switch away
        from it, and without looping mpv freezes on its last frame."""
        self._write('only.mp4')
        outcome, client = self._run([
            {'id': 'a', 'media_file': 'gone1.mp4'},
            {'id': 'b', 'media_file': 'only.mp4'},
            {'id': 'c', 'media_file': 'gone2.mp4'},
        ])
        self.assertEqual(outcome, disp.PLAYBACK_STARTED)
        self.assertTrue(client.start_mpv.call_args.kwargs['loop'])

    def test_single_scene_loops(self):
        self._write('only.mp4')
        _, client = self._run([{'id': 'a', 'media_file': 'only.mp4'}])
        self.assertTrue(client.start_mpv.call_args.kwargs['loop'])

    def test_all_files_present_behaves_exactly_as_before(self):
        """The healthy player: first scene, no looping with two scenes."""
        self._write('one.mp4'); self._write('two.mp4')
        outcome, client = self._run([
            {'id': 'a', 'media_file': 'one.mp4'},
            {'id': 'b', 'media_file': 'two.mp4'},
        ])
        self.assertEqual(outcome, disp.PLAYBACK_STARTED)
        self.assertFalse(client.start_mpv.call_args.kwargs['loop'])
        self.assertEqual(
            client.start_mpv.call_args.kwargs['initial_file'],
            str(self.media / 'one.mp4'),
        )

    def test_a_malformed_media_file_skips_one_scene_and_never_raises(self):
        """An uncaught raise here reaches run()'s bare try and exits the
        process; five of those inside 300 s leave the unit failed and dark."""
        self._write('good.mp4')
        outcome, client = self._run([
            {'id': 'bad', 'media_file': {'not': 'a path'}},
            {'id': 'good', 'media_file': 'good.mp4'},
        ])
        self.assertEqual(outcome, disp.PLAYBACK_STARTED)
        self.assertEqual(
            client.start_mpv.call_args.kwargs['initial_file'],
            str(self.media / 'good.mp4'),
        )

    def test_only_malformed_scenes_is_no_playable_file(self):
        outcome, _ = self._run([{'id': 'bad', 'media_file': 12345}])
        self.assertEqual(outcome, disp.PLAYBACK_NO_PLAYABLE_FILE)
        self.assertIsNone(self.mgr.mpv)


@unittest.skipUnless(HAVE_DISPLAY, DEVICE_ONLY)
class WatchdogAndBackoffTests(unittest.TestCase):

    def setUp(self):
        self.mgr = _bare_manager()

    def test_a_wait_always_pings_the_watchdog(self):
        with mock.patch.object(disp, 'sd_notifier') as notifier, \
             mock.patch.object(disp.time, 'sleep') as slept:
            self.mgr._sleep_with_watchdog(disp.WATCHDOG_SLEEP_SLICE_SEC * 3)
        self.assertGreaterEqual(notifier.notify.call_count, 4)
        for call in notifier.notify.call_args_list:
            self.assertEqual(call.args[0], 'WATCHDOG=1')
        self.assertGreaterEqual(slept.call_count, 3)
        for call in slept.call_args_list:
            self.assertLessEqual(call.args[0], disp.WATCHDOG_SLEEP_SLICE_SEC)

    def test_a_wait_is_cut_short_by_shutdown(self):
        self.mgr.running = False
        with mock.patch.object(disp, 'sd_notifier'), \
             mock.patch.object(disp.time, 'sleep') as slept:
            self.mgr._sleep_with_watchdog(300)
        slept.assert_not_called()

    def test_zero_wait_still_pings(self):
        with mock.patch.object(disp, 'sd_notifier') as notifier, \
             mock.patch.object(disp.time, 'sleep'):
            self.mgr._sleep_with_watchdog(0)
        notifier.notify.assert_called_once_with('WATCHDOG=1')

    def test_a_crash_burst_reports_and_never_shells_out(self):
        with mock.patch.object(disp.subprocess, 'run',
                               side_effect=AssertionError('must not run a command')), \
             mock.patch.object(disp.logger, 'error') as err:
            for _ in range(self.mgr._mpv_crash_threshold):
                self.mgr._note_mpv_crash()
        err.assert_called_once()
        self.assertIn('NOT restarting lightdm', err.call_args.args[0])
        self.assertEqual(len(self.mgr._mpv_crash_times), self.mgr._mpv_crash_threshold,
                         'the window is kept so the caller can keep throttling')

    def test_note_returns_the_burst_size_for_throttling(self):
        with mock.patch.object(disp.logger, 'error'):
            counts = [self.mgr._note_mpv_crash() for _ in range(3)]
        self.assertEqual(counts, [1, 2, 3], 'first exit is 1 -> the caller does not wait')

    def test_old_exits_fall_out_of_the_window(self):
        base = 1_000_000.0
        with mock.patch.object(disp.logger, 'error'), \
             mock.patch.object(disp.time, 'time', return_value=base):
            self.mgr._note_mpv_crash()
        with mock.patch.object(disp.logger, 'error'), \
             mock.patch.object(disp.time, 'time',
                               return_value=base + self.mgr._mpv_crash_window_seconds + 1):
            self.assertEqual(self.mgr._note_mpv_crash(), 1)

    def test_crashes_spread_out_do_not_report(self):
        base = 1_000_000.0
        with mock.patch.object(disp.logger, 'error') as err:
            for i in range(self.mgr._mpv_crash_threshold * 2):
                with mock.patch.object(disp.time, 'time',
                                       return_value=base + i * (self.mgr._mpv_crash_window_seconds + 1)):
                    self.mgr._note_mpv_crash()
        err.assert_not_called()

    def test_the_trouble_log_is_throttled_per_condition(self):
        """One fault must not hide a different one in the Logs & Errors panel."""
        with mock.patch.object(disp.logger, 'error') as err:
            self.mgr._log_display_trouble('mpv_start', 'cannot start')
            self.mgr._log_display_trouble('mpv_start', 'cannot start again')
            self.mgr._log_display_trouble('mpv_crash_burst', 'keeps crashing')
        self.assertEqual([c.args[0] for c in err.call_args_list],
                         ['cannot start', 'keeps crashing'])

    def test_bounds_are_sane(self):
        self.assertGreater(disp.MPV_RESTART_BACKOFF_MIN_SEC, 0)
        self.assertGreater(disp.MPV_RESTART_BACKOFF_MAX_SEC, disp.MPV_RESTART_BACKOFF_MIN_SEC)
        # A single wait must never approach WatchdogSec=60 without a ping, and
        # must not delay the main loop's mode re-evaluation for long.
        self.assertLessEqual(disp.WATCHDOG_SLEEP_SLICE_SEC, 20)
        self.assertLessEqual(disp.MPV_RESTART_BACKOFF_MAX_SEC, disp.STATE_CHECK_INTERVAL_SEC * 3)
        self.assertLessEqual(disp.MPV_CRASH_BURST_MAX_WAIT_SEC, 10)
        self.assertGreater(disp.MPV_CRASH_BURST_MAX_WAIT_SEC, 0)

    def test_the_outcome_constants_are_distinct(self):
        outcomes = {disp.PLAYBACK_STARTED, disp.PLAYBACK_NOTHING_SCHEDULED,
                    disp.PLAYBACK_NO_PLAYABLE_FILE, disp.PLAYBACK_MPV_FAILED}
        self.assertEqual(len(outcomes), 4)
        for outcome in outcomes:
            self.assertTrue(outcome, 'every outcome is truthy: never test one with `if`')


if __name__ == '__main__':
    unittest.main()
