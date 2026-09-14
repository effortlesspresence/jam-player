"""
The boot guard that keeps a broken updater from bricking the fleet.

jam_venv_repair runs before jam-update on the SYSTEM python and imports nothing
from common/, so it is the one thing that can recover when jam_update.py or
common/ no longer import. These tests exercise it with temp directories in
place of /opt/jam and /var/lib/jam; the import smoke and venv health are
mocked. Stdlib-only, so this runs on a laptop as well as a player.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_venv_repair as g  # noqa: E402

GOOD = "import os\n\ndef main():\n    return os.getcwd()\n"
BAD_SYNTAX = "def main(:\n    pass\n"


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.services = root / 'services'; (self.services / 'common').mkdir(parents=True)
        self.lkg = root / 'updater-lkg'; (self.lkg / 'common').mkdir(parents=True)
        self.state = root / 'state'
        (self.services / 'jam_update.py').write_text(GOOD)
        (self.services / 'common' / 'paths.py').write_text(GOOD)
        (self.lkg / 'jam_update.py').write_text(GOOD + "# lkg\n")
        (self.lkg / 'common' / 'paths.py').write_text(GOOD + "# lkg\n")
        (self.lkg / 'lkg_version.txt').write_text('abc123def456')
        self._p = [
            mock.patch.object(g, 'SERVICES', self.services),
            mock.patch.object(g, 'LKG_DIR', self.lkg),
            mock.patch.object(g, 'STATE_DIR', self.state),
            mock.patch.object(g, 'ATTEMPTS_FILE', self.state / 'updater_attempts'),
            mock.patch.object(g, 'RECOVERED_FLAG', self.state / 'updater_recovered'),
            mock.patch.object(g, 'log'),
        ]
        for p in self._p: p.start()
    def tearDown(self):
        for p in self._p: p.stop()
        self.tmp.cleanup()
    def _restored(self):
        return (self.services / 'jam_update.py').read_text().endswith('# lkg\n')


class GuardTests(_Sandbox):

    def test_healthy_updater_only_arms_the_attempt_counter(self):
        with mock.patch.object(g, '_installed_updater_imports', return_value=True):
            g._updater_guard()
        self.assertEqual(g._read_attempts(), 1)
        self.assertFalse(self._restored())
        self.assertFalse((self.state / 'updater_recovered').exists())

    def test_syntax_error_in_common_restores_lkg_and_flags_it(self):
        (self.services / 'common' / 'paths.py').write_text(BAD_SYNTAX)
        with mock.patch.object(g, '_installed_updater_imports', side_effect=AssertionError('must not import broken code')):
            g._updater_guard()
        self.assertTrue(self._restored())
        self.assertEqual((self.services / 'common' / 'paths.py').read_text(), GOOD + "# lkg\n")
        flag = (self.state / 'updater_recovered').read_text()
        self.assertIn('does not compile', flag)
        self.assertIn('lkg_version=abc123def456', flag)
        self.assertEqual(g._read_attempts(), 0, 'restored updater gets a fresh count')

    def test_import_failure_restores_lkg(self):
        with mock.patch.object(g, '_installed_updater_imports', return_value=False):
            g._updater_guard()
        self.assertTrue(self._restored())
        self.assertIn('fails to import', (self.state / 'updater_recovered').read_text())

    def test_unknown_import_result_because_venv_is_unhealthy_does_not_restore(self):
        """A broken venv is venv repair's problem, not a code problem."""
        with mock.patch.object(g, '_installed_updater_imports', return_value=None):
            g._updater_guard()
        self.assertFalse(self._restored())
        self.assertEqual(g._read_attempts(), 1)

    def test_two_boots_without_completion_restores_even_if_it_imports(self):
        """The runtime-crash case: imports fine, dies later, never records completion."""
        g._write_attempts(2)
        with mock.patch.object(g, '_installed_updater_imports', return_value=True):
            g._updater_guard()
        self.assertTrue(self._restored())
        self.assertIn('2 boots in a row', (self.state / 'updater_recovered').read_text())

    def test_one_incomplete_boot_is_not_yet_broken(self):
        g._write_attempts(1)
        with mock.patch.object(g, '_installed_updater_imports', return_value=True):
            g._updater_guard()
        self.assertFalse(self._restored())
        self.assertEqual(g._read_attempts(), 2)

    def test_identical_lkg_never_loops_on_restores(self):
        for f in ('jam_update.py',): (self.lkg / f).write_text(GOOD)
        (self.lkg / 'common' / 'paths.py').write_text(GOOD)
        (self.services / 'jam_update.py').write_text(GOOD)
        g._write_attempts(5)
        with mock.patch.object(g, '_installed_updater_imports', return_value=True), \
             mock.patch.object(g, '_restore_from_lkg', side_effect=AssertionError('must not restore an identical LKG')):
            g._updater_guard()
        self.assertEqual(g._read_attempts(), 6)

    def test_no_lkg_snapshot_is_reported_not_fatal(self):
        (self.lkg / 'jam_update.py').unlink()
        (self.services / 'jam_update.py').write_text(BAD_SYNTAX)
        g._updater_guard()   # must not raise
        self.assertFalse((self.state / 'updater_recovered').exists())

    def test_import_smoke_uses_the_venv_python_against_the_installed_tree(self):
        with mock.patch.object(g, 'VENV_PY', Path('/fake/venv/bin/python')), \
             mock.patch.object(Path, 'exists', return_value=True), \
             mock.patch.object(g, 'venv_ok', return_value=True), \
             mock.patch.object(g.subprocess, 'run', return_value=mock.MagicMock(returncode=0, stderr='')) as run:
            self.assertTrue(g._installed_updater_imports())
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], '/fake/venv/bin/python')
        self.assertIn('import jam_update', cmd[-1])
        self.assertIn(str(self.services), cmd[-1])


if __name__ == '__main__':
    unittest.main()
