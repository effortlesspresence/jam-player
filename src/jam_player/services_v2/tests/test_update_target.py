"""
Which commit should a player run? The stdlib decision logic behind the fleet
release control (common.update_target), on a laptop or a player.

The eligibility bucket must match the backend byte for byte; the two vectors
below were computed independently with hashlib and are the same numbers the
backend's tests assert.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import update_target as ut  # noqa: E402

ZERO_UUID = '00000000-0000-0000-0000-000000000000'
SAMPLE_UUID = '3f2a9c10-7b4e-4d2a-9e1f-0a1b2c3d4e5f'
HEAD = 'a' * 40


class EligibilityBucketTests(unittest.TestCase):

    def test_known_vectors_match_the_backend(self):
        self.assertEqual(ut.eligibility_bucket(ZERO_UUID), 52)
        self.assertEqual(ut.eligibility_bucket(SAMPLE_UUID), 47)

    def test_bucket_is_stable_and_case_and_whitespace_insensitive(self):
        self.assertEqual(ut.eligibility_bucket(SAMPLE_UUID), ut.eligibility_bucket(f'  {SAMPLE_UUID.upper()} '))

    def test_bucket_is_in_range(self):
        for i in range(200):
            self.assertTrue(0 <= ut.eligibility_bucket(f'{i:08x}-0000-0000-0000-000000000000') < 100)

    def test_is_eligible_is_strictly_less_than(self):
        self.assertTrue(ut.is_eligible(SAMPLE_UUID, 48))    # 47 < 48
        self.assertFalse(ut.is_eligible(SAMPLE_UUID, 47))   # 47 < 47 is False
        self.assertTrue(ut.is_eligible(SAMPLE_UUID, 100))
        self.assertFalse(ut.is_eligible(SAMPLE_UUID, 0))


class DecideUpdateTests(unittest.TestCase):

    def _t(self, **kw):
        base = {'branch': 'main', 'targetCommit': 'b' * 40, 'hold': False, 'eligiblePercent': 25,
                'eligible': False, 'stableCommit': 'c' * 40, 'followHead': False}
        base.update(kw); return base

    def test_no_answer_stays_put_never_head(self):
        """Fail CLOSED: a backend outage at 3 AM must not become an unstaged
        fleet-wide install of whatever sits at HEAD."""
        self.assertEqual(ut.decide_update(HEAD, None, installed_commit='z' * 40),
                         (None, ut.REASON_NO_ANSWER))
        self.assertEqual(ut.decide_update(HEAD, {}, installed_commit='z' * 40),
                         (None, ut.REASON_NO_ANSWER))

    def test_hold_freezes_both_pointers(self):
        self.assertEqual(ut.decide_update(HEAD, self._t(hold=True, eligible=True)), (None, ut.REASON_HOLD))

    def test_no_release_targeted_stays_put_by_default(self):
        """The human-error trap: a target created for main without a release
        must NOT push the branch tip to the whole fleet in one night."""
        self.assertEqual(ut.decide_update(HEAD, self._t(targetCommit=None), installed_commit='z' * 40),
                         (None, ut.REASON_NO_TARGET))
        t = self._t(targetCommit=None); del t['followHead']          # backend without the field
        self.assertEqual(ut.decide_update(HEAD, t, installed_commit='z' * 40),
                         (None, ut.REASON_NO_TARGET))
        self.assertEqual(ut.decide_update(HEAD, self._t(targetCommit=None, followHead='yes'),
                                          installed_commit='z' * 40),
                         (None, ut.REASON_NO_TARGET), 'only a real boolean True opts in')

    def test_no_release_with_follow_head_opt_in_follows_head(self):
        """Bench branches only; the backend refuses the opt-in on main."""
        self.assertEqual(ut.decide_update(HEAD, self._t(targetCommit=None, followHead=True)),
                         (HEAD, ut.REASON_FOLLOW_HEAD))

    def test_follow_head_is_ignored_while_a_release_is_targeted(self):
        self.assertEqual(ut.decide_update(HEAD, self._t(followHead=True, eligible=True)),
                         ('b' * 40, ut.REASON_ELIGIBLE))

    def test_eligible_takes_the_target(self):
        self.assertEqual(ut.decide_update(HEAD, self._t(eligible=True)), ('b' * 40, ut.REASON_ELIGIBLE))

    def test_not_eligible_but_already_on_the_target_stays_put(self):
        """Lowering the knob must NEVER move a player backwards. A device that
        already installed the target keeps it; only devices that never took it
        fall back to the stable floor."""
        self.assertEqual(
            ut.decide_update(HEAD, self._t(eligible=False), installed_commit='b' * 40),
            (None, ut.REASON_ALREADY_ON_TARGET))

    def test_already_on_target_matches_a_short_release_sha(self):
        """Releases may be cut with a 7-char SHA; version.txt holds the full 40."""
        self.assertEqual(
            ut.decide_update(HEAD, self._t(eligible=False, targetCommit='b' * 7),
                             installed_commit='b' * 40),
            (None, ut.REASON_ALREADY_ON_TARGET))

    def test_a_device_on_some_other_commit_still_falls_back_to_stable(self):
        self.assertEqual(
            ut.decide_update(HEAD, self._t(eligible=False), installed_commit='z' * 40),
            ('c' * 40, ut.REASON_STABLE))

    def test_eligible_still_takes_the_target_when_installed_is_passed(self):
        self.assertEqual(
            ut.decide_update(HEAD, self._t(eligible=True), installed_commit='z' * 40),
            ('b' * 40, ut.REASON_ELIGIBLE))

    def test_not_eligible_takes_stable_never_stands_still(self):
        """The warehouse device: months in a box, first connect, not in the canary
        cohort -> lands on the proven release, never on year-old image firmware."""
        self.assertEqual(ut.decide_update(HEAD, self._t(eligible=False)), ('c' * 40, ut.REASON_STABLE))

    def test_not_eligible_and_no_stable_yet_stays_put(self):
        self.assertEqual(
            ut.decide_update(HEAD, self._t(eligible=False, stableCommit=None), installed_commit='z' * 40),
            (None, ut.REASON_STAY))

    def test_missing_eligible_flag_is_recomputed_locally(self):
        t = self._t(eligiblePercent=48); del t['eligible']          # SAMPLE bucket 47 -> eligible
        self.assertEqual(ut.decide_update(HEAD, t, device_uuid=SAMPLE_UUID)[1], ut.REASON_ELIGIBLE)
        t = self._t(eligiblePercent=47); del t['eligible']          # 47 -> not eligible
        self.assertEqual(ut.decide_update(HEAD, t, device_uuid=SAMPLE_UUID)[1], ut.REASON_STABLE)


class FirstInstallTests(unittest.TestCase):
    """A player with NOTHING installed must always be given something.

    This is the 1.0 -> 2.0 migration path: the 1.0 installer clones this repo,
    enables jam-update and leaves the actual install to it, with no
    /etc/jam/version.txt. Returning "stay put" there would strand a 1.0 player
    on 1.0 forever the moment a branch has no release targeted -- the DEFAULT
    state of a branch.
    """

    def _t(self, **kw):
        base = {'branch': 'main', 'targetCommit': 'b' * 40, 'hold': False, 'eligiblePercent': 25,
                'eligible': False, 'stableCommit': 'c' * 40, 'followHead': False}
        base.update(kw); return base

    def test_no_release_targeted_still_installs_something(self):
        self.assertEqual(
            ut.decide_update(HEAD, self._t(targetCommit=None), installed_commit=None),
            ('c' * 40, ut.REASON_FIRST_INSTALL_STABLE))

    def test_prefers_stable_then_target_then_head(self):
        self.assertEqual(
            ut.decide_update(HEAD, self._t(targetCommit=None, stableCommit=None), installed_commit=None),
            (HEAD, ut.REASON_FIRST_INSTALL_HEAD))
        self.assertEqual(
            ut.decide_update(HEAD, self._t(eligible=False, stableCommit=None), installed_commit=None),
            ('b' * 40, ut.REASON_FIRST_INSTALL_TARGET))

    def test_backend_unreachable_still_installs_head(self):
        """A 1.0 player has no 2.0 code to protect; fail-closed would strand it."""
        self.assertEqual(ut.decide_update(HEAD, None, installed_commit=None),
                         (HEAD, ut.REASON_FIRST_INSTALL_HEAD))

    def test_hold_still_wins_even_for_a_first_install(self):
        """Hold is the emergency stop. It freezes migrations too, on purpose."""
        self.assertEqual(ut.decide_update(HEAD, self._t(hold=True), installed_commit=None),
                         (None, ut.REASON_HOLD))

    def test_an_eligible_first_install_takes_the_target_normally(self):
        self.assertEqual(ut.decide_update(HEAD, self._t(eligible=True), installed_commit=None),
                         ('b' * 40, ut.REASON_ELIGIBLE))

    def test_nothing_known_at_all_changes_nothing(self):
        self.assertEqual(ut.decide_update(None, None, installed_commit=None),
                         (None, ut.REASON_NO_ANSWER))

    def test_an_installed_player_is_never_given_the_floor(self):
        for kwargs in ({'targetCommit': None}, {'eligible': False, 'stableCommit': None}):
            commit, _ = ut.decide_update(HEAD, self._t(**kwargs), installed_commit='z' * 40)
            self.assertIsNone(commit, f'installed players must still be able to stay put: {kwargs}')


class CommitsMatchTests(unittest.TestCase):
    """Short-SHA-aware comparison; the backend accepts 7..64 hex for a release."""

    def test_exact_and_abbreviated_match(self):
        full = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
        self.assertTrue(ut.commits_match(full, full))
        self.assertTrue(ut.commits_match(full[:7], full))
        self.assertTrue(ut.commits_match(full, full[:12]))
        self.assertTrue(ut.commits_match(full.upper(), f'  {full}  '))

    def test_non_matches(self):
        full = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678'
        self.assertFalse(ut.commits_match(full, 'b1b2c3d4e5f6'))
        self.assertFalse(ut.commits_match(None, full))
        self.assertFalse(ut.commits_match(full, ''))
        self.assertFalse(ut.commits_match('a1b2c3', full), 'under 7 chars is not an abbreviation')


class CachedTargetTests(unittest.TestCase):
    """The last good answer survives a reboot and is only reused for ITS branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state' / 'update_target.json'   # parent must be created
        self.answer = {'targetCommit': 'b' * 40, 'hold': False, 'eligiblePercent': 25,
                       'eligible': True, 'stableCommit': None, 'followHead': False}

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_same_branch(self):
        self.assertTrue(ut.write_cached_target('main', self.answer, path=self.path))
        got = ut.read_cached_target('main', path=self.path)
        self.assertIsNotNone(got)
        answer, fetched_at = got
        self.assertEqual(answer, self.answer)
        self.assertRegex(fetched_at, r'^\d{4}-\d{2}-\d{2}T')
        self.assertFalse(self.path.with_name(self.path.name + '.tmp').exists(), 'atomic: no tmp left behind')

    def test_other_branch_is_not_reused(self):
        ut.write_cached_target('testing', self.answer, path=self.path)
        self.assertIsNone(ut.read_cached_target('main', path=self.path))

    def test_missing_corrupt_or_empty_cache_is_none(self):
        self.assertIsNone(ut.read_cached_target('main', path=self.path))
        self.path.parent.mkdir(parents=True); self.path.write_text('{not json')
        self.assertIsNone(ut.read_cached_target('main', path=self.path))
        self.path.write_text('{"branch": "main", "answer": {}}')
        self.assertIsNone(ut.read_cached_target('main', path=self.path))

    def test_write_refuses_non_dict(self):
        self.assertFalse(ut.write_cached_target('main', None, path=self.path))
        self.assertFalse(self.path.exists())

    def test_cached_hold_still_holds_when_decided(self):
        ut.write_cached_target('main', dict(self.answer, hold=True), path=self.path)
        answer, _ = ut.read_cached_target('main', path=self.path)
        self.assertEqual(ut.decide_update(HEAD, answer), (None, ut.REASON_HOLD))


class ReadUpdateBranchTests(unittest.TestCase):

    def _with(self, content):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'environment'
            if content is not None:
                f.write_text(content)
            with mock.patch.object(ut, 'ENVIRONMENT_FILE', f):
                return ut.read_update_branch()

    def test_missing_prod_false_all_mean_main(self):
        for c in (None, '', 'prod', 'PROD', 'false', '  '):
            self.assertEqual(self._with(c), 'main', repr(c))

    def test_any_other_value_is_the_branch(self):
        self.assertEqual(self._with('testing\n'), 'testing')
        self.assertEqual(self._with('staging'), 'staging')


if __name__ == '__main__':
    unittest.main()
