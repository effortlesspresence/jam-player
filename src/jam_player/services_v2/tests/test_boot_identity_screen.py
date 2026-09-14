"""
The device-identity screen shown at every boot, and the Plymouth splash.

Two requirements, both easy to get subtly wrong:

  * It must appear once per BOOT, not once per service start. This unit
    restarts on watchdog kills, crashes and every update; re-holding the screen
    for 15 s on each of those would delay the customer's content exactly when
    the player is already struggling. The marker lives in /run, which the
    kernel clears at boot.
  * It must never fight another service for the display, and never become a
    gate. Every failure path skips and lets the normal modes take over.

The splash installer replaces the shipped JAM logo only once a player has a
render good enough to cache, keeps the logo so it can be restored, and writes
only when the bytes actually change (this runs every boot; the SD card is the
scarcest resource on the device).

Runs on a JAM Player: importing the display module needs the on-device venv.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
for candidate in (_HERE.parents[1], _HERE.parents[2]):
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
UUID = "3f2a9c10-7b4e-4d2a-9e1f-0a1b2c3d4e5f"


def _manager(tmp: Path):
    mgr = disp.JamPlayerDisplayManager.__new__(disp.JamPlayerDisplayManager)
    mgr.running = True
    mgr.mpv = None
    mgr.is_playing = False
    mgr.feh_process = None
    mgr.screen_width = 1920
    mgr.screen_height = 1080
    mgr._mpv_crash_times = []
    mgr._mpv_crash_threshold = 5
    mgr._mpv_crash_window_seconds = 30
    mgr._mpv_restart_backoff_sec = disp.MPV_RESTART_BACKOFF_MIN_SEC
    mgr._display_trouble_log_sec = {}
    return mgr


@unittest.skipUnless(HAVE_DISPLAY, DEVICE_ONLY)
class BootIdentityRenderTests(unittest.TestCase):

    def test_no_uuid_renders_nothing(self):
        """A freshly imaged player has nothing to identify, so the shipped logo
        splash stays and the hold is skipped entirely."""
        img, cacheable = disp.create_boot_identity_screen(1920, 1080, None)
        self.assertIsNone(img)
        self.assertFalse(cacheable)

    def test_renders_and_is_cacheable_when_macs_are_readable(self):
        with mock.patch.object(disp, '_get_display_macs',
                               return_value={'wifiMac': 'AA:BB:CC:DD:EE:FF',
                                             'ethernetMac': 'AA:BB:CC:DD:EE:11'}):
            img, cacheable = disp.create_boot_identity_screen(1920, 1080, UUID)
        self.assertIsNotNone(img)
        self.assertTrue(cacheable)
        self.assertEqual(img.size, (1920, 1080))

    def test_not_cacheable_when_no_mac_can_be_read(self):
        """A MAC-less PNG must not be cached, and must never become the splash."""
        with mock.patch.object(disp, '_get_display_macs',
                               return_value={'wifiMac': None, 'ethernetMac': None}):
            img, cacheable = disp.create_boot_identity_screen(1920, 1080, UUID)
        self.assertIsNotNone(img)
        self.assertFalse(cacheable)

    def test_render_is_fast_enough_for_the_boot_path(self):
        """Flat background on purpose: the mesh gradient takes 10-15 s at 4K."""
        import time as _t
        with mock.patch.object(disp, '_get_display_macs',
                               return_value={'wifiMac': 'AA:BB:CC:DD:EE:FF', 'ethernetMac': None}):
            start = _t.monotonic()
            img, _ = disp.create_boot_identity_screen(3840, 2160, UUID)
            elapsed = _t.monotonic() - start
        self.assertIsNotNone(img)
        self.assertLess(elapsed, 5.0, f"4K render took {elapsed:.1f}s; the boot hold is only "
                                      f"{disp.BOOT_IDENTITY_HOLD_SECONDS}s")

    def test_it_is_registered_so_jam_update_prewarms_it(self):
        """Registered purely so jam-update renders it at install time; a cache
        miss on the boot path would otherwise cost the customer seconds."""
        registry = disp.build_display_render_registry()
        self.assertIn(disp.BOOT_IDENTITY_CACHE_KEY, registry)
        self.assertIs(registry[disp.BOOT_IDENTITY_CACHE_KEY], disp.create_boot_identity_screen)


@unittest.skipUnless(HAVE_DISPLAY, DEVICE_ONLY)
class InstallBootSplashTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.theme = root / 'themes' / 'pix'
        self.theme.mkdir(parents=True)
        self.splash = self.theme / 'splash.png'
        self.backup = self.theme / 'splash.jam-logo.png'
        self.source = root / 'rendered.png'
        self.source.write_bytes(b'IDENTITY-SCREEN')
        self._p = [mock.patch.object(disp, 'PLYMOUTH_SPLASH', self.splash),
                   mock.patch.object(disp, 'PLYMOUTH_SPLASH_BACKUP', self.backup)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def test_replaces_the_logo_and_keeps_it(self):
        self.splash.write_bytes(b'JAM-LOGO')
        self.assertTrue(disp.install_boot_splash(str(self.source)))
        self.assertEqual(self.splash.read_bytes(), b'IDENTITY-SCREEN')
        self.assertEqual(self.backup.read_bytes(), b'JAM-LOGO', 'the shipped logo must stay restorable')
        self.assertEqual(list(self.theme.glob('*.jam-tmp')), [], 'no temp file left behind')

    def test_identical_bytes_are_not_rewritten(self):
        """Runs every boot; the steady state must be a read, not an SD write."""
        self.splash.write_bytes(b'IDENTITY-SCREEN')
        before = self.splash.stat().st_mtime_ns
        self.assertTrue(disp.install_boot_splash(str(self.source)))
        self.assertEqual(self.splash.stat().st_mtime_ns, before)
        self.assertFalse(self.backup.exists(), 'nothing changed, so nothing to back up')

    def test_the_logo_backup_is_never_overwritten(self):
        self.splash.write_bytes(b'JAM-LOGO')
        disp.install_boot_splash(str(self.source))
        self.source.write_bytes(b'SECOND-RENDER')
        disp.install_boot_splash(str(self.source))
        self.assertEqual(self.splash.read_bytes(), b'SECOND-RENDER')
        self.assertEqual(self.backup.read_bytes(), b'JAM-LOGO')

    def test_an_empty_render_is_refused(self):
        self.splash.write_bytes(b'JAM-LOGO')
        self.source.write_bytes(b'')
        self.assertFalse(disp.install_boot_splash(str(self.source)))
        self.assertEqual(self.splash.read_bytes(), b'JAM-LOGO')

    def test_no_plymouth_theme_is_a_clean_no_op(self):
        for p in self._p:
            p.stop()
        missing = Path(self.tmp.name) / 'no-such-theme' / 'splash.png'
        with mock.patch.object(disp, 'PLYMOUTH_SPLASH', missing), \
             mock.patch.object(disp, 'PLYMOUTH_SPLASH_BACKUP', missing.with_name('b.png')):
            self.assertFalse(disp.install_boot_splash(str(self.source)))
        for p in self._p:
            p.start()

    def test_a_missing_source_never_raises(self):
        self.assertFalse(disp.install_boot_splash(str(Path(self.tmp.name) / 'gone.png')))


@unittest.skipUnless(HAVE_DISPLAY, DEVICE_ONLY)
class ShowOncePerBootTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flag = Path(self.tmp.name) / 'run' / 'boot_identity_shown'
        self.mgr = _manager(Path(self.tmp.name))
        self.proc = mock.MagicMock(pid=4242)
        self._p = [
            mock.patch.object(disp, 'BOOT_IDENTITY_SHOWN_FLAG', self.flag),
            mock.patch.object(disp, 'get_device_uuid', return_value=UUID),
            mock.patch.object(disp, 'get_or_render_cached',
                              return_value=str(disp.DISPLAY_CACHE_DIR / 'boot_identity.png')),
            mock.patch.object(disp, 'display_path_with_feh', return_value=self.proc),
            mock.patch.object(disp, 'install_boot_splash', return_value=True),
            mock.patch.object(disp, 'UPDATE_IN_PROGRESS_FLAG', Path(self.tmp.name) / 'no-update'),
            mock.patch.object(self.mgr, '_sleep_with_watchdog'),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def test_happy_path_shows_holds_marks_and_installs_the_splash(self):
        self.mgr._show_boot_identity_screen_once()
        disp.display_path_with_feh.assert_called_once()
        self.mgr._sleep_with_watchdog.assert_called_once_with(disp.BOOT_IDENTITY_HOLD_SECONDS)
        disp.install_boot_splash.assert_called_once()
        self.assertIs(self.mgr.feh_process, self.proc, 'tracked so the first transition tears it down')
        self.assertTrue(self.flag.exists())

    def test_second_start_in_the_same_boot_does_nothing(self):
        self.mgr._show_boot_identity_screen_once()
        disp.display_path_with_feh.reset_mock()
        self.mgr._sleep_with_watchdog.reset_mock()
        self.mgr._show_boot_identity_screen_once()
        disp.display_path_with_feh.assert_not_called()
        self.mgr._sleep_with_watchdog.assert_not_called()

    def test_no_uuid_skips_and_still_marks(self):
        with mock.patch.object(disp, 'get_device_uuid', return_value=None):
            self.mgr._show_boot_identity_screen_once()
        disp.display_path_with_feh.assert_not_called()
        self.assertTrue(self.flag.exists(), 'must not re-try later in the same boot')

    def test_an_update_in_progress_skips_and_still_marks(self):
        update_flag = Path(self.tmp.name) / 'update-running'
        update_flag.touch()
        with mock.patch.object(disp, 'UPDATE_IN_PROGRESS_FLAG', update_flag), \
             mock.patch.object(disp, '_is_update_flag_stale', return_value=False), \
             mock.patch.object(disp, '_jam_update_service_is_active', return_value=True):
            self.mgr._show_boot_identity_screen_once()
        disp.display_path_with_feh.assert_not_called()
        self.assertTrue(self.flag.exists())

    def test_a_failed_render_skips_and_still_marks(self):
        with mock.patch.object(disp, 'get_or_render_cached', return_value=None):
            self.mgr._show_boot_identity_screen_once()
        disp.display_path_with_feh.assert_not_called()
        self.mgr._sleep_with_watchdog.assert_not_called()
        self.assertTrue(self.flag.exists())

    def test_a_failed_feh_skips_the_hold_and_still_marks(self):
        with mock.patch.object(disp, 'display_path_with_feh', return_value=None):
            self.mgr._show_boot_identity_screen_once()
        self.mgr._sleep_with_watchdog.assert_not_called()
        self.assertTrue(self.flag.exists())

    def test_a_tmp_fallback_render_never_becomes_the_splash(self):
        """A /tmp path means the render was not cacheable -- no MACs, or the
        cache is broken. That must not become what every future boot shows."""
        with mock.patch.object(disp, 'get_or_render_cached', return_value='/tmp/jam_display_boot_identity.png'):
            self.mgr._show_boot_identity_screen_once()
        self.mgr._sleep_with_watchdog.assert_called_once()
        disp.install_boot_splash.assert_not_called()

    def test_an_unexpected_error_still_marks_and_never_propagates(self):
        with mock.patch.object(disp, 'get_or_render_cached', side_effect=RuntimeError('boom')):
            self.mgr._show_boot_identity_screen_once()   # must not raise
        self.assertTrue(self.flag.exists())

    def test_the_hold_is_bounded_well_under_the_watchdog(self):
        self.assertGreater(disp.BOOT_IDENTITY_HOLD_SECONDS, 0)
        self.assertLess(disp.BOOT_IDENTITY_HOLD_SECONDS, 60)


if __name__ == '__main__':
    unittest.main()
