"""
The updater must not be able to brick itself, and it must run the commit the
backend says -- not blindly branch HEAD.

Covers jam_update's: staging + validation of a NEW updater before promotion
(real subprocess on the venv python against a real temp tree), the
last-known-good snapshot/restore, git residue hygiene and corruption
detection, the release-target resolution end to end, and the boot-guard
stamps. Runs on a JAM Player (jam_update imports the venv); git/HTTP are mocked.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jam_update as ju  # noqa: E402

GOOD_UPDATER = "import os\nVERSION = 'x'\n\ndef main():\n    return os.getcwd()\n"
GOOD_COMMON = "def helper():\n    return 1\n"


def _tree(root: Path, updater=GOOD_UPDATER, common=GOOD_COMMON, extra=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'jam_update.py').write_text(updater)
    (root / 'common').mkdir(exist_ok=True)
    (root / 'common' / '__init__.py').write_text('')
    (root / 'common' / 'paths.py').write_text(common)
    for name, text in (extra or {}).items():
        (root / name).write_text(text)


class ValidatorTests(unittest.TestCase):
    """Real subprocess, real files: the validator runs the venv python against a staged tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.staging = Path(self.tmp.name) / 'staging'
    def tearDown(self):
        self.tmp.cleanup()

    def test_good_tree_passes(self):
        _tree(self.staging)
        ok, why = ju._run_validator(self.staging)
        self.assertTrue(ok, why)

    def test_syntax_error_fails(self):
        _tree(self.staging, common="def broken(:\n    pass\n")
        ok, why = ju._run_validator(self.staging)
        self.assertFalse(ok); self.assertIn('SyntaxError', why)

    def test_undefined_name_fails_before_import(self):
        """The F821 class that shipped in 69b46b6: caught statically, named in the reason."""
        _tree(self.staging, common="def tick():\n    return adapter.Get('x')\n")
        ok, why = ju._run_validator(self.staging)
        self.assertFalse(ok); self.assertIn("undefined name 'adapter'", why)

    def test_missing_third_party_module_fails_with_module_not_found(self):
        _tree(self.staging, updater="import definitely_not_installed_xyz\n\ndef main():\n    pass\n")
        ok, why = ju._run_validator(self.staging)
        self.assertFalse(ok); self.assertIn('ModuleNotFoundError', why)


class StagedValidationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.repo_src = root / 'services_v2'; _tree(self.repo_src)
        self.staging = root / 'staging'
        self._p = [mock.patch.object(ju, 'SERVICES_V2_SRC', self.repo_src),
                   mock.patch.object(ju, 'UPDATER_STAGING_DIR', self.staging)]
        for p in self._p: p.start()
    def tearDown(self):
        for p in self._p: p.stop(); self.tmp.cleanup()

    def test_stages_validates_and_cleans_up(self):
        ok, why = ju._validate_staged_updater()
        self.assertTrue(ok, why)
        self.assertFalse(self.staging.exists(), 'staging dir must not linger')

    def test_missing_module_triggers_one_dependency_install_then_revalidates(self):
        calls = iter([(False, "ModuleNotFoundError: No module named 'newdep'"), (True, '')])
        with mock.patch.object(ju, '_run_validator', side_effect=lambda st: next(calls)), \
             mock.patch.object(ju, 'install_dependencies') as deps:
            ok, why = ju._validate_staged_updater()
        self.assertTrue(ok); deps.assert_called_once()

    def test_other_failures_do_not_install_dependencies(self):
        with mock.patch.object(ju, '_run_validator', return_value=(False, 'SyntaxError: bad')), \
             mock.patch.object(ju, 'install_dependencies', side_effect=AssertionError('no install')):
            ok, why = ju._validate_staged_updater()
        self.assertFalse(ok)


class LkgSnapshotTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.services = root / 'services'; _tree(self.services, extra={'venv_check.py': 'X = 1\n'})
        self.lkg = root / 'updater-lkg'
        self._p = [mock.patch.object(ju, 'SERVICES_DEST', self.services),
                   mock.patch.object(ju, 'UPDATER_LKG_DIR', self.lkg),
                   mock.patch.object(ju, 'get_current_version', return_value='abc123')]
        for p in self._p: p.start()
    def tearDown(self):
        for p in self._p: p.stop(); self.tmp.cleanup()

    def test_snapshot_captures_updater_common_and_version(self):
        self.assertTrue(ju._snapshot_updater_lkg())
        self.assertEqual((self.lkg / 'jam_update.py').read_text(), GOOD_UPDATER)
        self.assertEqual((self.lkg / 'common' / 'paths.py').read_text(), GOOD_COMMON)
        self.assertEqual((self.lkg / 'venv_check.py').read_text(), 'X = 1\n')
        self.assertEqual((self.lkg / 'lkg_version.txt').read_text().strip(), 'abc123')

    def test_second_snapshot_replaces_atomically_leaving_no_scratch_dirs(self):
        ju._snapshot_updater_lkg()
        (self.services / 'jam_update.py').write_text(GOOD_UPDATER + '# v2\n')
        self.assertTrue(ju._snapshot_updater_lkg())
        self.assertTrue((self.lkg / 'jam_update.py').read_text().endswith('# v2\n'))
        self.assertFalse(self.lkg.with_name('updater-lkg.new').exists())
        self.assertFalse(self.lkg.with_name('updater-lkg.old').exists())

    def test_restore_copies_lkg_over_the_installed_tree(self):
        ju._snapshot_updater_lkg()
        (self.services / 'jam_update.py').write_text('BROKEN(')
        (self.services / 'common' / 'paths.py').write_text('BROKEN(')
        n = ju._restore_updater_from_lkg()
        self.assertGreaterEqual(n, 3)
        self.assertEqual((self.services / 'jam_update.py').read_text(), GOOD_UPDATER)
        self.assertEqual((self.services / 'common' / 'paths.py').read_text(), GOOD_COMMON)

    def test_restore_without_snapshot_is_a_noop(self):
        self.assertEqual(ju._restore_updater_from_lkg(), 0)


class GitHygieneTests(unittest.TestCase):

    def test_sweep_removes_only_stale_locks_and_old_temp_packs(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / 'repo'; git = repo / '.git'
            (git / 'refs' / 'remotes' / 'origin').mkdir(parents=True); (git / 'objects' / 'pack').mkdir(parents=True)
            stale = git / 'index.lock'; stale.write_text(''); old = time.time() - 3600; os.utime(stale, (old, old))
            fresh = git / 'refs' / 'remotes' / 'origin' / 'main.lock'; fresh.write_text('')
            pack_old = git / 'objects' / 'pack' / 'tmp_pack_abc'; pack_old.write_text(''); os.utime(pack_old, (old - 86400, old - 86400))
            pack_new = git / 'objects' / 'pack' / 'tmp_pack_def'; pack_new.write_text('')
            with mock.patch.object(ju, 'JAM_REPO_DIR', repo):
                ju._sweep_git_residue()
            self.assertFalse(stale.exists()); self.assertTrue(fresh.exists(), 'a lock from an in-flight git must survive')
            self.assertFalse(pack_old.exists()); self.assertTrue(pack_new.exists())

    def test_corruption_markers(self):
        self.assertTrue(ju._looks_like_git_corruption('fatal: bad object HEAD'))
        self.assertTrue(ju._looks_like_git_corruption('error: object file .git/objects/ab/cd is empty\nfatal: loose object abcd (stored in ...) is corrupt'))
        self.assertFalse(ju._looks_like_git_corruption('fatal: unable to access: Could not resolve host'))


class ReleaseTargetResolutionTests(unittest.TestCase):
    HEAD = 'h' * 40

    INSTALLED = 'i' * 40   # an installed player; the first-install floor is test_update_target's business

    def _resolve(self, target, uuid='3f2a9c10-7b4e-4d2a-9e1f-0a1b2c3d4e5f', available=True,
                 cached=None, installed=INSTALLED, is_ancestor=False):
        """target = what the backend answers (None = unreachable); cached = what
        /var/lib/jam/update_target.json holds for 'main' (None = nothing);
        installed = /etc/jam/version.txt; is_ancestor = what
        `git merge-base --is-ancestor <desired> <installed>` answers."""
        def run(cmd, cwd=None, timeout=120):
            if cmd[:2] == ['git', 'fetch']: return (True, '', '')
            if cmd[:2] == ['git', 'rev-parse']: return (True, self.HEAD + '\n', '')
            if cmd[:3] == ['git', 'merge-base', '--is-ancestor']: return (is_ancestor, '', '')
            return (True, '', '')
        self.cache_writes = []
        with mock.patch.object(ju, 'get_current_version', return_value=installed), \
             mock.patch.object(ju, 'run_command', side_effect=run), \
             mock.patch.object(ju, '_sweep_git_residue'), \
             mock.patch.object(ju, 'retry_with_backoff', side_effect=lambda fn, name, **kw: fn()), \
             mock.patch.object(ju, '_fetch_update_target', return_value=target), \
             mock.patch.object(ju, 'read_cached_target', return_value=(cached, '2026-09-13T03:00:00') if cached else None), \
             mock.patch.object(ju, 'write_cached_target', side_effect=lambda b, a: self.cache_writes.append((b, a)) or True), \
             mock.patch('common.credentials.get_device_uuid', return_value=uuid), \
             mock.patch.object(ju, '_ensure_commit_available', return_value=available), \
             mock.patch.object(ju, 'report_error') as report_error:
            self.report_error = report_error
            return ju.get_latest_version('main')

    def test_no_backend_answer_and_no_cache_stays_put_never_head(self):
        """Fail CLOSED: an outage at 3 AM must not install the branch tip fleet-wide."""
        self.assertIsNone(self._resolve(None))

    def test_no_backend_answer_uses_the_cached_decision(self):
        self.assertEqual(self._resolve(None, cached={'targetCommit': 't' * 40, 'hold': False, 'eligible': True}), 't' * 40)

    def test_cached_hold_stays_held_through_an_outage(self):
        self.assertIsNone(self._resolve(None, cached={'targetCommit': 't' * 40, 'hold': True, 'eligible': True}))

    def test_fresh_answer_is_cached_for_its_branch(self):
        answer = {'targetCommit': 't' * 40, 'hold': False, 'eligible': True}
        self._resolve(answer)
        self.assertEqual(self.cache_writes, [('main', answer)])

    def test_no_release_targeted_stays_put_by_default(self):
        self.assertIsNone(self._resolve({'targetCommit': None, 'hold': False, 'eligible': True, 'followHead': False}))

    def test_no_release_with_follow_head_opt_in_follows_head(self):
        self.assertEqual(self._resolve({'targetCommit': None, 'hold': False, 'eligible': True, 'followHead': True}), self.HEAD)

    def test_an_update_handed_over_mid_flight_is_finished_not_re_decided(self):
        """The fielded-fleet path. Every updater before release control pulls
        the branch tip, promotes this file plus common/, and re-execs with
        version.txt still naming the OLD commit. Re-deciding here would read
        that stale file, decide the player is already where it belongs, and
        abandon the update -- forever, since every later boot repeats it."""
        checked_out = 'c' * 40

        def run(cmd, cwd=None, timeout=120):
            if cmd == ['git', 'rev-parse', 'HEAD']:
                return (True, checked_out + '\n', '')
            raise AssertionError(f'must not reach {cmd[:3]} while finishing an in-flight update')

        with mock.patch.dict(ju.os.environ, {ju.REEXEC_ENV_VAR: '1'}), \
             mock.patch.object(ju, 'run_command', side_effect=run), \
             mock.patch.object(ju, '_lookup_update_target',
                               side_effect=AssertionError('must not ask the backend mid-flight')), \
             mock.patch.object(ju, '_sweep_git_residue',
                               side_effect=AssertionError('must not re-fetch mid-flight')):
            self.assertEqual(ju.get_latest_version('main'), checked_out)

    def test_a_normal_run_is_unaffected_by_the_absent_reexec_flag(self):
        env = {k: v for k, v in ju.os.environ.items() if k != ju.REEXEC_ENV_VAR}
        with mock.patch.dict(ju.os.environ, env, clear=True):
            self.assertEqual(
                self._resolve({'targetCommit': 't' * 40, 'hold': False, 'eligible': True}),
                't' * 40)

    def test_an_unreadable_head_mid_flight_falls_back_to_a_normal_decision(self):
        """Never strand the player because one git call failed."""
        def run(cmd, cwd=None, timeout=120):
            if cmd == ['git', 'rev-parse', 'HEAD']:
                return (False, '', 'fatal: not a git repository')
            if cmd[:2] == ['git', 'fetch']:
                return (True, '', '')
            if cmd[:2] == ['git', 'rev-parse']:
                return (True, self.HEAD + '\n', '')
            return (True, '', '')
        with mock.patch.dict(ju.os.environ, {ju.REEXEC_ENV_VAR: '1'}), \
             mock.patch.object(ju, 'run_command', side_effect=run), \
             mock.patch.object(ju, '_sweep_git_residue'), \
             mock.patch.object(ju, 'retry_with_backoff', side_effect=lambda fn, name, **kw: fn()), \
             mock.patch.object(ju, '_fetch_update_target', return_value=None), \
             mock.patch.object(ju, 'read_cached_target', return_value=None), \
             mock.patch.object(ju, 'write_cached_target', return_value=True), \
             mock.patch.object(ju, 'get_current_version', return_value=self.INSTALLED), \
             mock.patch('common.credentials.get_device_uuid', return_value='u'), \
             mock.patch.object(ju, 'report_error'):
            self.assertIsNone(ju.get_latest_version('main'), 'no answer, no cache -> stay put')

    def test_lowering_the_knob_does_not_move_a_device_that_already_took_the_target(self):
        """The scary path: eligiblePercent lowered, and devices that already
        installed the new release get pulled back to stable. They must not."""
        self.assertIsNone(self._resolve(
            {'targetCommit': 't' * 40, 'hold': False, 'eligible': False, 'stableCommit': 's' * 40},
            installed='t' * 40))

    def test_a_backward_target_is_refused_outright(self):
        """Even a deliberate backward target is refused: the updater never
        deletes files or units a newer release added, so 'downgrading' yields a
        mixed-version tree. Fix forward instead."""
        self.assertIsNone(self._resolve(
            {'targetCommit': 'old' + 'a' * 37, 'hold': False, 'eligible': True},
            installed='n' * 40, is_ancestor=True))
        rep = self.report_error   # _resolve patches report_error itself; assert on that mock
        self.assertTrue(rep.called)
        self.assertIn('older than the installed', rep.call_args.args[0])

    def test_a_forward_move_is_allowed(self):
        self.assertEqual(
            self._resolve({'targetCommit': 't' * 40, 'hold': False, 'eligible': True},
                          installed='o' * 40, is_ancestor=False),
            't' * 40)

    def test_an_unrelated_history_is_not_treated_as_backwards(self):
        """A bench unit switching branches: neither commit is an ancestor."""
        self.assertEqual(
            self._resolve({'targetCommit': 't' * 40, 'hold': False, 'eligible': True},
                          installed='u' * 40, is_ancestor=False),
            't' * 40)

    def test_hold_means_no_update(self):
        self.assertIsNone(self._resolve({'targetCommit': 't' * 40, 'hold': True, 'eligible': True}))

    def test_eligible_runs_the_target(self):
        self.assertEqual(self._resolve({'targetCommit': 't' * 40, 'hold': False, 'eligible': True}), 't' * 40)

    def test_not_eligible_runs_stable(self):
        self.assertEqual(self._resolve({'targetCommit': 't' * 40, 'hold': False, 'eligible': False, 'stableCommit': 's' * 40}), 's' * 40)

    def test_unobtainable_commit_is_skipped_not_guessed(self):
        self.assertIsNone(self._resolve({'targetCommit': 't' * 40, 'hold': False, 'eligible': True}, available=False))

    def test_pull_latest_resets_to_the_decided_commit(self):
        with mock.patch.object(ju, 'run_command', return_value=(True, '', '')) as run:
            self.assertTrue(ju.pull_latest('c' * 40))
        self.assertEqual(run.call_args.args[0][:4], ['git', 'reset', '--hard', 'c' * 40])


class GuardStampTests(unittest.TestCase):

    def test_controlled_exit_clears_attempts_and_recovery_flag_is_reported_once(self):
        with tempfile.TemporaryDirectory() as d:
            attempts = Path(d) / 'updater_attempts'; attempts.write_text('1')
            flag = Path(d) / 'updater_recovered'; flag.write_text('reason=test\nlkg_version=abc\n')
            with mock.patch.object(ju, 'UPDATER_ATTEMPTS_FILE', attempts), \
                 mock.patch.object(ju, 'UPDATER_RECOVERED_FLAG', flag), \
                 mock.patch.object(ju, 'report_error') as rep:
                ju._clear_updater_attempts()
                ju._report_guard_recovery_if_any()
                ju._report_guard_recovery_if_any()   # second call: nothing left to report
            self.assertFalse(attempts.exists()); self.assertFalse(flag.exists())
            rep.assert_called_once(); self.assertIn('reason=test', rep.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
