"""
Nothing from the Pi OS desktop may draw a window on the customer's TV.

The JAM display runs inside the Pi OS desktop session. That session starts the
desktop panel (wf-panel-pi); the image hides the panel, but its applets keep
running, and two of them were seen on the bench opening windows over the JAM
screen:

  2026-11-15  "Authentication required by Wi-Fi network" (network applet),
              after a reboot with a saved password that did not work.
  2026-11-16  "Connection successful", one OK button, while the phone changed
              the Wi-Fi of a registered player that was playing content.

jam-update now rewrites the panel's settings so it loads no applets at all
(one blank spacer on each side) and stops the running panel once, which the
desktop starts again without them. These tests pin the rewrite and the
install. The real settings file from the bench player is the main fixture.

That the panel then really has no network or Bluetooth applet loaded can only
be seen on a device: BENCH_TESTS.md, case A8.
"""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_update  # noqa: E402

# ~/.config/wf-panel-pi.ini exactly as read off a bench player on 5148277.
BENCH_FILE = (
    "[panel]\n"
    "launcher_000001=lxde-x-www-browser.desktop\n"
    "launcher_000002=pcmanfm.desktop\n"
    "launcher_000003=lxterminal.desktop\n"
    "autohide=true\n"
    "autohide_duration=100\n"
    "notify_enable=false\n"
    "notify_timeout=5\n"
    "widgets_left=smenu spacing4 launchers spacing8 window-list \n"
    "widgets_right=tray power ejecter spacing2 bluetooth spacing2 netman spacing2 "
    "volumepulse spacing2 clock spacing2 batt spacing2  connect\n"
    "position=top\n"
    "icon_size=8\n"
    "window-list_max_width=200\n"
    "monitor=0\n"
)

# Every applet the bench file loaded. None may survive.
APPLETS = ('smenu', 'launchers', 'window-list', 'tray', 'power', 'ejecter', 'bluetooth',
           'netman', 'volumepulse', 'clock', 'batt', 'connect', 'updater')


def _settings(content, section='[panel]'):
    """key -> value for one section, the way the panel reads it."""
    found, inside = {}, False
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            inside = stripped == section
            continue
        if inside and '=' in stripped and not stripped.startswith('#'):
            key, value = stripped.split('=', 1)
            found[key.strip()] = value.strip()
    return found


def _loaded_applets(content):
    panel = _settings(content)
    words = (panel.get('widgets_left', '') + ' ' + panel.get('widgets_right', '')).split()
    return [w for w in words if not w.startswith('spacing')]


class StripAppletsTests(unittest.TestCase):

    def test_the_bench_file_ends_up_with_no_applets(self):
        out = jam_update._strip_panel_applets(BENCH_FILE)
        self.assertEqual(_loaded_applets(out), [])
        panel = _settings(out)
        self.assertEqual(panel['widgets_left'], 'spacing4')
        self.assertEqual(panel['widgets_right'], 'spacing4')
        self.assertEqual(panel['notify_enable'], 'false')
        for applet in ('netman', 'bluetooth'):
            self.assertNotIn(applet, panel['widgets_left'] + panel['widgets_right'])

    def test_every_other_line_is_left_exactly_as_it_was(self):
        before = BENCH_FILE.split('\n')
        after = jam_update._strip_panel_applets(BENCH_FILE).split('\n')
        self.assertEqual(len(after), len(before))
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        self.assertEqual([a for _, a in changed], ['widgets_left=spacing4', 'widgets_right=spacing4'])
        # The panel stays hidden: those settings are the image's and are kept.
        self.assertIn('autohide=true', after)
        self.assertIn('icon_size=8', after)

    def test_it_is_idempotent(self):
        once = jam_update._strip_panel_applets(BENCH_FILE)
        self.assertEqual(jam_update._strip_panel_applets(once), once)

    def test_a_spacer_is_left_never_an_empty_list(self):
        # An empty value could mean "use the defaults" to the panel, and the
        # defaults load everything.
        panel = _settings(jam_update._strip_panel_applets(BENCH_FILE))
        self.assertTrue(panel['widgets_left'])
        self.assertTrue(panel['widgets_right'])

    def test_keys_written_with_spaces_are_recognised(self):
        content = "[panel]\nwidgets_left = smenu\nwidgets_right = bluetooth netman\nnotify_enable = true\n"
        out = jam_update._strip_panel_applets(content)
        self.assertEqual(_loaded_applets(out), [])
        self.assertEqual(_settings(out)['notify_enable'], 'false')
        self.assertEqual(out.count('widgets_right'), 1, 'replaced in place, not duplicated')

    def test_notifications_are_switched_off(self):
        out = jam_update._strip_panel_applets(BENCH_FILE.replace('notify_enable=false', 'notify_enable=true'))
        self.assertEqual(_settings(out)['notify_enable'], 'false')

    def test_missing_keys_are_added_inside_the_panel_section(self):
        # With no widgets_* line the panel uses its defaults: every applet.
        content = "[panel]\nautohide=true\n\n[other]\nwidgets_right=netman\n"
        out = jam_update._strip_panel_applets(content)
        panel = _settings(out)
        self.assertEqual(panel['widgets_left'], 'spacing4')
        self.assertEqual(panel['widgets_right'], 'spacing4')
        self.assertEqual(panel['notify_enable'], 'false')
        self.assertEqual(panel['autohide'], 'true')
        # The other section is not ours to edit.
        self.assertEqual(_settings(out, '[other]'), {'widgets_right': 'netman'})
        self.assertEqual(jam_update._strip_panel_applets(out), out)

    def test_a_file_without_a_panel_section_gets_one(self):
        out = jam_update._strip_panel_applets("[something]\nkey=value\n")
        self.assertEqual(_settings(out, '[something]'), {'key': 'value'})
        panel = _settings(out)
        self.assertEqual(_loaded_applets(out), [])
        self.assertEqual(panel['autohide'], 'true', 'a panel we create must be hidden')
        self.assertEqual(jam_update._strip_panel_applets(out), out)

    def test_an_empty_or_missing_file_becomes_a_hidden_empty_panel(self):
        for content in ('', '\n', '   \n\n'):
            out = jam_update._strip_panel_applets(content)
            panel = _settings(out)
            self.assertEqual(_loaded_applets(out), [])
            self.assertEqual(panel['autohide'], 'true')
            self.assertEqual(panel['notify_enable'], 'false')
            self.assertEqual(jam_update._strip_panel_applets(out), out)

    def test_commented_lines_are_not_settings(self):
        content = "[panel]\n# widgets_right=netman\nwidgets_right=netman\n"
        out = jam_update._strip_panel_applets(content)
        self.assertIn('# widgets_right=netman', out.split('\n'))
        self.assertEqual(_settings(out)['widgets_right'], 'spacing4')

    def test_no_applet_name_is_left_in_what_the_panel_loads(self):
        out = jam_update._strip_panel_applets(BENCH_FILE)
        loaded = _settings(out)['widgets_left'] + ' ' + _settings(out)['widgets_right']
        for applet in APPLETS:
            self.assertNotIn(applet, loaded.split())


class InstallTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmp.name) / 'home' / '.config' / 'wf-panel-pi.ini'
        self.commands = []

        def fake_run(cmd, cwd=None, timeout=120):
            self.commands.append(list(cmd))
            return True, '', ''

        self.patches = [
            mock.patch.object(jam_update, '_PANEL_CONF', self.conf),
            mock.patch.object(jam_update, 'run_command', side_effect=fake_run),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write(self, content):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(content)

    def _restarts(self):
        return [c for c in self.commands if c[0] == 'pkill']

    def test_the_bench_file_is_rewritten_and_the_panel_restarted_once(self):
        self._write(BENCH_FILE)
        self.assertTrue(jam_update.install_kiosk_panel_config())
        self.assertEqual(_loaded_applets(self.conf.read_text()), [])
        self.assertEqual(self._restarts(), [['pkill', '-u', 'comitup', '-x', 'wf-panel-pi']])

    def test_the_file_goes_back_to_the_desktop_user(self):
        self._write(BENCH_FILE)
        jam_update.install_kiosk_panel_config()
        chowns = [c for c in self.commands if c[0] == 'chown']
        self.assertIn(['chown', 'comitup:comitup', str(self.conf)], chowns)

    def test_a_second_run_changes_nothing_and_restarts_nothing(self):
        self._write(BENCH_FILE)
        jam_update.install_kiosk_panel_config()
        before = self.conf.stat().st_mtime_ns
        self.commands.clear()
        self.assertFalse(jam_update.install_kiosk_panel_config())
        self.assertEqual(self.conf.stat().st_mtime_ns, before)
        self.assertEqual(self.commands, [], 'the panel must not be restarted on every update')

    def test_a_player_with_no_settings_file_gets_a_hidden_empty_panel(self):
        self.assertFalse(self.conf.exists())
        self.assertTrue(jam_update.install_kiosk_panel_config())
        self.assertEqual(_loaded_applets(self.conf.read_text()), [])
        self.assertEqual(_settings(self.conf.read_text())['autohide'], 'true')

    def test_no_half_written_file_is_left_behind(self):
        self._write(BENCH_FILE)
        jam_update.install_kiosk_panel_config()
        self.assertEqual([p.name for p in self.conf.parent.iterdir()], ['wf-panel-pi.ini'])

    def test_a_restart_that_fails_does_not_fail_the_update(self):
        self._write(BENCH_FILE)

        def failing(cmd, cwd=None, timeout=120):
            if cmd[0] == 'pkill':
                raise RuntimeError('pkill blew up')
            return True, '', ''

        with mock.patch.object(jam_update, 'run_command', side_effect=failing):
            self.assertTrue(jam_update.install_kiosk_panel_config())
        self.assertEqual(_loaded_applets(self.conf.read_text()), [])

    def test_an_unreadable_file_does_not_fail_the_update(self):
        self._write(BENCH_FILE)
        with mock.patch.object(Path, 'read_text', side_effect=OSError('card error')):
            self.assertFalse(jam_update.install_kiosk_panel_config())
        self.assertEqual(self._restarts(), [])

    def test_only_the_panel_is_touched(self):
        # Restarting NetworkManager, Bluetooth or the display here would drop
        # the link the update runs over, or blank the TV.
        self._write(BENCH_FILE)
        jam_update.install_kiosk_panel_config()
        self.assertEqual(sorted({c[0] for c in self.commands}), ['chown', 'pkill'])

    def test_every_update_runs_it(self):
        self.assertIn('install_kiosk_panel_config()', inspect.getsource(jam_update.main))


if __name__ == '__main__':
    unittest.main()
