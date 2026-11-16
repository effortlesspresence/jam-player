"""
Plugging a USB drive into a player must put nothing on the customer's TV.

Found on the bench on 2026-11-16 (player on b05231f): a USB drive brought up
the desktop file manager's box over the content --

    Removable medium is inserted
    Type of medium: removable disk
    Select the action you want to perform:
    Open in File Manager                      [Cancel] [OK]

The file manager (pcmanfm) runs in the desktop session to draw the desktop,
and at its defaults it mounts any drive and asks what to do with it. Nobody
on site has a keyboard to dismiss the box, and OK opens a file manager on a
device that holds its own keys. jam-update now switches both off in the
desktop user's settings file and restarts the file manager once.

These tests pin the rewrite and the install. That a drive then really shows
nothing can only be seen on a device: BENCH_TESTS.md, case A9.
"""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_update  # noqa: E402

# The shape of /etc/xdg/pcmanfm/LXDE-pi/pcmanfm.conf. The [volume] block is
# exactly as read off the bench player (it starts on line 5 there too); the
# sections around it stand in for the rest of the file.
SYSTEM_FILE = (
    "[config]\n"
    "bm_open_method=0\n"
    "su_cmd=xdg-su -c '%s'\n"
    "\n"
    "[volume]\n"
    "mount_on_startup=1\n"
    "mount_removable=1\n"
    "autorun=1\n"
    "\n"
    "[ui]\n"
    "always_show_tabs=0\n"
    "hide_close_btn=0\n"
)

OFF = {'mount_on_startup': '0', 'mount_removable': '0', 'autorun': '0'}


def _settings(content, section='[volume]'):
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


class RewriteTests(unittest.TestCase):

    def test_mounting_and_the_question_are_both_switched_off(self):
        self.assertEqual(_settings(jam_update._ignore_removable_media(SYSTEM_FILE)), OFF)

    def test_every_other_line_is_left_exactly_as_it_was(self):
        before = SYSTEM_FILE.split('\n')
        after = jam_update._ignore_removable_media(SYSTEM_FILE).split('\n')
        self.assertEqual(len(after), len(before))
        changed = [a for b, a in zip(before, after) if b != a]
        self.assertEqual(changed, ['mount_on_startup=0', 'mount_removable=0', 'autorun=0'])
        out = '\n'.join(after)
        self.assertEqual(_settings(out, '[config]'), _settings(SYSTEM_FILE, '[config]'))
        self.assertEqual(_settings(out, '[ui]'), _settings(SYSTEM_FILE, '[ui]'))

    def test_it_is_idempotent(self):
        once = jam_update._ignore_removable_media(SYSTEM_FILE)
        self.assertEqual(jam_update._ignore_removable_media(once), once)

    def test_a_key_that_is_missing_is_added_to_the_volume_section(self):
        # A missing key means the built-in default, which is "on".
        content = "[volume]\nmount_on_startup=0\n\n[ui]\nautorun=1\n"
        out = jam_update._ignore_removable_media(content)
        self.assertEqual(_settings(out), OFF)
        self.assertEqual(_settings(out, '[ui]'), {'autorun': '1'}, 'another section is not ours to edit')
        self.assertEqual(jam_update._ignore_removable_media(out), out)

    def test_a_file_without_a_volume_section_gets_one(self):
        out = jam_update._ignore_removable_media("[config]\nbm_open_method=0\n")
        self.assertEqual(_settings(out), OFF)
        self.assertEqual(_settings(out, '[config]'), {'bm_open_method': '0'})
        self.assertEqual(jam_update._ignore_removable_media(out), out)

    def test_an_empty_file_becomes_just_the_volume_section(self):
        out = jam_update._ignore_removable_media('')
        self.assertEqual(out, '[volume]\nmount_on_startup=0\nmount_removable=0\nautorun=0\n')

    def test_keys_written_with_spaces_are_recognised(self):
        out = jam_update._ignore_removable_media("[volume]\nautorun = 1\nmount_removable = 1\nmount_on_startup = 1\n")
        self.assertEqual(_settings(out), OFF)
        self.assertEqual(out.count('autorun'), 1, 'replaced in place, not duplicated')


class InstallTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home_config = root / 'home' / '.config'
        self.home_config.mkdir(parents=True)
        self.user = self.home_config / 'pcmanfm' / 'LXDE-pi' / 'pcmanfm.conf'
        self.system = root / 'etc' / 'xdg' / 'pcmanfm' / 'LXDE-pi' / 'pcmanfm.conf'
        self.system.parent.mkdir(parents=True)
        self.system.write_text(SYSTEM_FILE)
        self.commands = []

        def fake_run(cmd, cwd=None, timeout=120):
            self.commands.append(list(cmd))
            return True, '', ''

        self.patches = [
            mock.patch.object(jam_update, '_FILE_MANAGER_CONF', self.user),
            mock.patch.object(jam_update, '_FILE_MANAGER_SYSTEM_CONF', self.system),
            mock.patch.object(jam_update, 'run_command', side_effect=fake_run),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _kills(self):
        return [c for c in self.commands if c[0] == 'pkill']

    def test_the_bench_player_gets_a_user_file_made_from_the_system_one(self):
        # As found on the bench: system file says "on", no user file.
        self.assertFalse(self.user.exists())
        self.assertTrue(jam_update.install_ignore_removable_media())
        written = self.user.read_text()
        self.assertEqual(_settings(written), OFF)
        # Everything else the system file set is carried over unchanged.
        self.assertEqual(_settings(written, '[config]'), _settings(SYSTEM_FILE, '[config]'))
        self.assertEqual(_settings(written, '[ui]'), _settings(SYSTEM_FILE, '[ui]'))
        # The system file itself is not ours to edit.
        self.assertEqual(self.system.read_text(), SYSTEM_FILE)

    def test_the_file_manager_is_killed_once_not_asked_to_quit(self):
        # On a clean quit it saves the settings it has in memory, which would
        # put the old "on" values straight back over the file just written.
        jam_update.install_ignore_removable_media()
        self.assertEqual(self._kills(), [['pkill', '-KILL', '-u', 'comitup', '-x', 'pcmanfm']])

    def test_the_file_is_written_before_the_file_manager_is_killed(self):
        seen = []

        def record(cmd, cwd=None, timeout=120):
            if cmd[0] == 'pkill':
                seen.append(self.user.exists() and _settings(self.user.read_text()) == OFF)
            return True, '', ''

        with mock.patch.object(jam_update, 'run_command', side_effect=record):
            jam_update.install_ignore_removable_media()
        self.assertEqual(seen, [True], 'it must find the new settings when the desktop restarts it')

    def test_new_folders_and_the_file_go_to_the_desktop_user(self):
        jam_update.install_ignore_removable_media()
        chowned = [c[2] for c in self.commands if c[0] == 'chown']
        self.assertEqual(chowned, [str(self.user.parent.parent), str(self.user.parent), str(self.user)])
        self.assertTrue(all(c[1] == 'comitup:comitup' for c in self.commands if c[0] == 'chown'))

    def test_a_second_run_changes_nothing_and_kills_nothing(self):
        jam_update.install_ignore_removable_media()
        before = self.user.stat().st_mtime_ns
        self.commands.clear()
        self.assertFalse(jam_update.install_ignore_removable_media())
        self.assertEqual(self.user.stat().st_mtime_ns, before)
        self.assertEqual(self.commands, [], 'the file manager must not be killed on every update')

    def test_a_user_file_that_turned_it_back_on_is_corrected(self):
        self.user.parent.mkdir(parents=True)
        self.user.write_text("[volume]\nmount_on_startup=1\nmount_removable=1\nautorun=1\n\n[desktop]\nwallpaper_mode=crop\n")
        self.assertTrue(jam_update.install_ignore_removable_media())
        self.assertEqual(_settings(self.user.read_text()), OFF)
        self.assertEqual(_settings(self.user.read_text(), '[desktop]'), {'wallpaper_mode': 'crop'})
        self.assertEqual(len(self._kills()), 1)

    def test_an_image_whose_system_file_is_already_off_is_left_alone(self):
        self.system.write_text(jam_update._ignore_removable_media(SYSTEM_FILE))
        self.assertFalse(jam_update.install_ignore_removable_media())
        self.assertFalse(self.user.exists())
        self.assertEqual(self.commands, [])

    def test_a_player_with_no_settings_at_all_gets_the_three_keys(self):
        self.system.unlink()
        self.assertTrue(jam_update.install_ignore_removable_media())
        self.assertEqual(_settings(self.user.read_text()), OFF)

    def test_no_half_written_file_is_left_behind(self):
        jam_update.install_ignore_removable_media()
        self.assertEqual([p.name for p in self.user.parent.iterdir()], ['pcmanfm.conf'])

    def test_a_kill_that_fails_does_not_fail_the_update(self):
        def failing(cmd, cwd=None, timeout=120):
            if cmd[0] == 'pkill':
                raise RuntimeError('pkill blew up')
            return True, '', ''

        with mock.patch.object(jam_update, 'run_command', side_effect=failing):
            self.assertTrue(jam_update.install_ignore_removable_media())
        self.assertEqual(_settings(self.user.read_text()), OFF)

    def test_an_unreadable_file_does_not_fail_the_update(self):
        with mock.patch.object(Path, 'read_text', side_effect=OSError('card error')):
            self.assertFalse(jam_update.install_ignore_removable_media())
        self.assertEqual(self._kills(), [])

    def test_only_the_file_manager_is_touched(self):
        jam_update.install_ignore_removable_media()
        self.assertEqual(sorted({c[0] for c in self.commands}), ['chown', 'pkill'])
        self.assertTrue(all(c[-1] == 'pcmanfm' for c in self._kills()))

    def test_every_update_runs_it(self):
        self.assertIn('install_ignore_removable_media()', inspect.getsource(jam_update.main))


if __name__ == '__main__':
    unittest.main()
