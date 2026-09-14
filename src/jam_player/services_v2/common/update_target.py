"""
Which commit should this player run? Stdlib-only decision logic shared by
jam_update.py (the caller) and its tests.

Until 2026-09 a device ran whatever sat at the HEAD of its branch at its
nightly reboot, at 100% of the fleet, with no backend say. Now the backend
publishes, per branch, a release TARGET (the release being rolled out), how
much of the fleet is ELIGIBLE to take it, a STABLE floor (the last release
fully rolled out), and a hold switch. This module turns that answer into "run this commit" or
"do nothing this run". The transport (asking the backend) lives in
jam_update.py; the rules live here so they are testable on a laptop.

FAIL CLOSED. If the backend cannot be asked, the updater uses the last answer
it cached (write_cached_target / read_cached_target, persistent under
/var/lib/jam) and otherwise changes nothing. It never falls back to the branch
tip: a backend outage overlapping the 3 AM reboot must not become an unstaged
fleet-wide install of whatever sits at HEAD -- the exact event the eligibility
knob exists to prevent. "No release targeted" is likewise stay-put unless the
branch's target carries the explicit followHead opt-in (bench branches; the
backend refuses it on main).

The eligibility bucket MUST match the backend byte for byte -- see the doc
comment on JamPlayerReleaseTarget in the jam-sphere Prisma schema:
    int.from_bytes(sha256(device_uuid.lower())[:4], 'big') % 100 < eligiblePercent
Never a language hash(): Python's is randomised per process.
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .paths import ENVIRONMENT_FILE, UPDATE_TARGET_CACHE_FILE

GIT_BRANCH_DEFAULT = 'main'

# Reasons returned by decide_update (stable strings: logged and tested).
REASON_NO_ANSWER = 'no answer from the backend and no cached answer; staying put (fail closed)'
REASON_HOLD = 'updates on this branch are HELD by a JAM super-admin'
REASON_FOLLOW_HEAD = 'no release targeted and follow-HEAD is ON for this branch; following branch HEAD'
REASON_NO_TARGET = 'no release targeted for this branch and follow-HEAD is off; staying put'
REASON_ELIGIBLE = 'eligible for the current release target'
REASON_STABLE = 'not yet eligible for the current target; running the stable release instead'
REASON_STAY = 'not eligible for the current target and no stable release exists yet; staying put'
REASON_ALREADY_ON_TARGET = ('no longer eligible, but this device already installed the target; '
                           'staying put -- a player never moves backwards on its own')


def read_update_branch() -> str:
    """
    The git branch this device follows: the content of
    /etc/jam/config/environment unless it is empty, 'prod' or 'false', which
    all mean 'main'. Set during provisioning/migration; edited by hand on
    bench units ('testing', 'staging'). Reported to the backend with the
    installed version so the fleet views compare a device to ITS branch.
    """
    try:
        if ENVIRONMENT_FILE.exists():
            content = ENVIRONMENT_FILE.read_text().strip()
            if content and content.lower() not in ('false', 'prod'):
                return content
    except Exception:
        pass
    return GIT_BRANCH_DEFAULT


def eligibility_bucket(device_uuid: str) -> int:
    """0..99, stable per device, identical on the backend. Lower-cased and
    stripped first so formatting differences cannot move a device."""
    digest = hashlib.sha256(device_uuid.strip().lower().encode('utf-8')).digest()
    return int.from_bytes(digest[:4], 'big') % 100


def is_eligible(device_uuid: str, eligible_percent: int) -> bool:
    return eligibility_bucket(device_uuid) < int(eligible_percent)


def commits_match(a: Optional[str], b: Optional[str]) -> bool:
    """
    Do these two refer to the same commit? Prefix-aware, because a release may
    be cut with a short SHA (the backend accepts 7..64 hex) while
    /etc/jam/version.txt holds the full 40. Compares on the shorter length,
    case-insensitively. Below 7 characters nothing is a valid abbreviation, so
    only exact equality counts.
    """
    if not a or not b:
        return False
    a = str(a).strip().lower()
    b = str(b).strip().lower()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    if n < 7:
        return a == b
    return a[:n] == b[:n]


def decide_update(head_commit: Optional[str], target: Optional[Dict[str, Any]],
                  device_uuid: Optional[str] = None,
                  installed_commit: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Decide which commit this player should be on.

    Args:
        head_commit: the branch HEAD just fetched (None if the fetch failed).
        target: the backend's answer (fresh or cached), or None if there is
                none (FAIL-CLOSED: nothing changes this run).
        device_uuid: used to recompute eligibility if the answer lacks it.
        installed_commit: what this player is running right now. Used only to
                refuse to move BACKWARDS -- see the stable branch below.

    Returns (commit_or_None, reason). None means "do not change anything this
    run". The caller compares the commit to the installed version; equality
    means up to date. Precedence: hold > follow-HEAD (explicit opt-in
    only) > eligible > already-on-target > stable > stay. No answer, and no
    release targeted without the opt-in, both mean stay.
    """
    if not target:
        return None, REASON_NO_ANSWER
    if target.get('hold'):
        return None, REASON_HOLD
    target_commit = target.get('targetCommit')
    if not target_commit:
        if target.get('followHead') is True:
            return head_commit, REASON_FOLLOW_HEAD
        return None, REASON_NO_TARGET
    eligible = target.get('eligible')
    if eligible is None and device_uuid:
        try:
            eligible = is_eligible(device_uuid, int(target.get('eligiblePercent', 100)))
        except (TypeError, ValueError):
            eligible = False
    if eligible:
        return str(target_commit), REASON_ELIGIBLE
    # NOT eligible. The stable release is a FLOOR for players that have never
    # taken the target -- a unit unboxed after months in a warehouse lands on
    # the proven release instead of year-old image firmware. It is NOT a
    # rollback instruction. A player that already installed the target must
    # stay on it when the knob is lowered: lowering a percentage reads as
    # "slow the rollout down", and it must never silently move a slice of the
    # fleet backwards onto a different build. (Downgrades are also not clean
    # on this device: the updater deliberately never deletes files or systemd
    # units that a newer release added, so moving back yields old code plus
    # newer leftovers, not the old version.)
    if commits_match(installed_commit, target_commit):
        return None, REASON_ALREADY_ON_TARGET
    stable = target.get('stableCommit')
    if stable:
        return str(stable), REASON_STABLE
    return None, REASON_STAY


# ---------------------------------------------------------------------------
# Cached answer (fail closed). One small JSON file, atomically replaced.
# ---------------------------------------------------------------------------
CACHE_FORMAT_VERSION = 1


def write_cached_target(branch: str, answer: Dict[str, Any],
                        path: Path = UPDATE_TARGET_CACHE_FILE) -> bool:
    """Remember the backend's answer for `branch` so an outage at the next run
    reuses the admin's last decision instead of guessing. Atomic: tmp + fsync +
    os.replace, so a power cut leaves either the old file or the new one."""
    if not isinstance(answer, dict):
        return False
    payload = {
        'version': CACHE_FORMAT_VERSION,
        'branch': branch,
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'answer': answer,
    }
    tmp = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, 'w') as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def read_cached_target(branch: str,
                       path: Path = UPDATE_TARGET_CACHE_FILE) -> Optional[Tuple[Dict[str, Any], str]]:
    """The cached (answer, fetched_at) for exactly this branch, or None when
    there is no usable cache (missing, unreadable, another branch, wrong
    shape). A bench unit whose branch was switched by hand must not reuse the
    other branch's decision."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or data.get('branch') != branch:
        return None
    answer = data.get('answer')
    if not isinstance(answer, dict) or not answer:
        return None
    return answer, str(data.get('fetched_at') or 'unknown time')
