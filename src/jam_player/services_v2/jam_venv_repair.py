#!/usr/bin/env python3
"""
JAM Player venv self-heal  --  STDLIB ONLY.

Runs at boot BEFORE jam-update and the jam services. If /opt/jam/venv is
incomplete (a prior update/migration half-installed it -- e.g. a pip install
timed out on a flaky store network), this repairs it in place with the same
resilient, cache-resuming pip install, then clears the version marker so
jam-update re-runs its full pipeline cleanly.

WHY THIS EXISTS: the incident where two players bricked was caused by a
jam-update whose pip install timed out mid-download, leaving a venv missing
sdnotify/nacl. That broke not only the display service but jam-update ITSELF
(it imports nacl at module load), so nothing could self-heal. This script is
the one piece of recovery code that must keep working when the venv is broken.

HARD RULE: import NOTHING that lives in the venv this repairs -- no third-party
packages, no common.* modules. Standard library only, and shell out to the
venv interpreter/pip via subprocess. It runs on the SYSTEM python
(/usr/bin/python3), never the (possibly broken) venv python.
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

# Shared, stdlib-only completeness check. Ships in the same directory as this
# script, so it imports cleanly on the system python. requirements.txt is the
# single source of truth -- this module holds no dependency list of its own.
from venv_check import missing_requirements

OPT_JAM = Path('/opt/jam')
VENV = OPT_JAM / 'venv'
VENV_PY = VENV / 'bin' / 'python'
VENV_PIP = VENV / 'bin' / 'pip'
PIP_CACHE = OPT_JAM / 'pip-cache'
VERSION_FILE = Path('/etc/jam/version.txt')

# requirements.txt: prefer the installed copy, fall back to the pulled repo
# (covers a migration that died before copying requirements into place).
REQ_CANDIDATES = [
    OPT_JAM / 'services' / 'requirements.txt',
    Path('/home/comitup/jam-player/src/jam_player/services_v2/requirements.txt'),
]

# Bounded per-boot budget: 2 attempts x 10 min. The persistent pip cache means
# each attempt -- and each subsequent BOOT -- resumes from what's already
# downloaded, so a spotty network makes forward progress instead of restarting.
# Kept modest so the boot-time block (this is ordered Before= the jam services)
# stays bounded; if it can't finish, it defers to the next boot rather than
# hanging. NOTE: pynacl currently builds from source on the Pi (piwheels wheel
# name-case quirk), which is the slowest step -- 10 min covers it.
#
# BUDGET COUPLING: these two values feed TimeoutStartSec in
# jam-venv-repair.service. Worst-case runtime is roughly
#   2 * (PIP_SUBPROCESS_TIMEOUT + 60) + ~160   (repair + initial check + wait)
# and MUST stay well under the unit's TimeoutStartSec, or systemd will SIGTERM
# a repair mid-pip and strand a half-written venv -- the exact failure this
# script exists to prevent. Bump one, re-check the other.
REPAIR_MAX_ATTEMPTS = 2
PIP_SUBPROCESS_TIMEOUT = 600

# Bounded wait for outbound connectivity before attempting a repair. We must
# tell a brief WiFi blip (absorb it -- keep polling) apart from a genuinely
# offline device (give up in bounded time so we never hang boot). A SINGLE
# probe conflated the two: one unlucky blip during that one probe deferred the
# whole repair to the NEXT boot -- which for a fielded device is the nightly
# reboot, i.e. up to ~24h of a broken, unusable device. On the spotty-WiFi
# stores this whole fix targets, that blip is the expected condition, not an
# edge case. So we POLL: any connectivity within the window repairs on the
# spot; only a device dark for the ENTIRE window defers to the next boot.
NETWORK_WAIT_TOTAL_S = 90       # keep polling for connectivity up to this long
NETWORK_PROBE_TIMEOUT_S = 5     # per-host TCP-connect timeout
NETWORK_PROBE_INTERVAL_S = 5    # pause between poll rounds


def log(msg):
    # journald captures stdout; keep this dependency-free.
    print(f"[jam-venv-repair] {msg}", flush=True)


def venv_ok():
    """True iff the venv satisfies requirements.txt -- the single source of
    truth, checked via the shared venv_check (same logic jam_update uses)."""
    req = find_requirements()
    if req is None:
        log("no requirements.txt found -- cannot verify venv")
        return False
    missing = missing_requirements(VENV_PY, req)
    if missing:
        log(f"venv NOT complete -- missing: {missing}")
        return False
    return True


# NOTE: intentionally NOT common.network.wait_for_network(). That helper polls
# NetworkManager association state (nmcli) and lives in a module that can pull
# in third-party packages -- neither is usable here. This healer is STDLIB ONLY
# (it must run when the venv is broken), and it needs to know it can reach the
# PACKAGE INDEX specifically: nmcli reports "connected" even on a captive-portal
# WiFi with no route to PyPI -- the exact failure mode that bricked players. So
# we probe the actual package hosts, and this stays a self-contained ~8 lines
# rather than an import that would couple recovery to a mutable services module.
def _package_index_reachable(timeout):
    """Single probe: True if EITHER package host accepts a TCP connection on
    :443. A bare connect (no TLS/HTTP) is enough to distinguish 'the network can
    carry us to PyPI/piwheels' from 'we're dark'. All socket failures (DNS,
    refused, timeout, unreachable) are OSError subclasses."""
    for host, port in (('pypi.org', 443), ('www.piwheels.org', 443)):
        try:
            socket.create_connection((host, port), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def wait_for_package_index(total=NETWORK_WAIT_TOTAL_S,
                           probe_timeout=NETWORK_PROBE_TIMEOUT_S,
                           interval=NETWORK_PROBE_INTERVAL_S):
    """Poll until the Python package index is reachable, up to `total` seconds.
    Returns True the instant a probe succeeds; returns False only after the
    whole window elapses with no reachability.

    This absorbs a brief blip -- a failed probe just leads to the next one --
    instead of the old single-shot behavior where one unlucky blip deferred the
    repair to the next (nightly) boot. Still bounded, so a genuinely-offline
    device never hangs boot: it defers after `total`s and retries next cycle."""
    deadline = time.monotonic() + total
    probes = 0
    while True:
        probes += 1
        if _package_index_reachable(probe_timeout):
            if probes > 1:
                log(f"package index reachable after {probes} probes")
            return True
        if time.monotonic() >= deadline:
            log(f"package index unreachable after ~{total}s ({probes} probes)")
            return False
        time.sleep(interval)


def find_requirements():
    for p in REQ_CANDIDATES:
        if p.exists():
            return p
    return None


def recreate_venv():
    """Recreate /opt/jam/venv from scratch on the system python.

    Must mirror jam_update.ensure_venv_exists: --system-site-packages (the
    completeness check and dbus-python both depend on it). Bounded at 120s --
    venv creation is local disk work, a few seconds on any Pi."""
    try:
        r = subprocess.run(
            [sys.executable, '-m', 'venv', '--system-site-packages', str(VENV)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        log(f"venv creation errored: {e}")
        return False
    if r.returncode != 0:
        log(f"venv creation failed: {(r.stderr or '').strip()[-300:]}")
        return False
    return VENV_PIP.exists()


def repair():
    req = find_requirements()
    if req is None:
        log("no requirements.txt found -- cannot repair")
        return False
    if not VENV_PIP.exists():
        log("venv pip still missing after recreation attempt -- cannot repair this boot")
        return False

    PIP_CACHE.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(VENV_PIP), 'install',
        '--timeout', '120', '--retries', '10', '--prefer-binary',
        '--cache-dir', str(PIP_CACHE),
        '-r', str(req),
    ]
    for attempt in range(1, REPAIR_MAX_ATTEMPTS + 1):
        log(f"repair pip install attempt {attempt}/{REPAIR_MAX_ATTEMPTS} (from {req})")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=PIP_SUBPROCESS_TIMEOUT)
            if r.returncode != 0:
                log(f"pip attempt failed: {(r.stderr or '').strip()[-300:]}")
        except Exception as e:
            log(f"pip errored: {e}")
        # Re-check after every attempt -- the cache means each pass makes
        # forward progress even if the connection dropped.
        if venv_ok():
            log("venv now complete")
            try:
                VERSION_FILE.unlink(missing_ok=True)
                log("cleared version marker so jam-update re-runs cleanly")
            except Exception as e:
                log(f"could not clear version marker (non-fatal): {e}")
            return True
    return False


def _attempt_self_heal():
    """The self-heal flow. Separated from main() so main() can wrap it in a
    catch-all that upholds the 'always exit 0' invariant no matter what."""
    if venv_ok():
        return 0  # healthy: get out of the boot path immediately
    log("venv INCOMPLETE -- attempting self-heal")
    if not VENV_PIP.exists():
        # The venv itself is gone or gutted (not merely missing packages).
        # Deferring to jam-update here used to be CIRCULAR: jam-update's own
        # ExecStart interpreter IS the venv python, so with the venv missing
        # (e.g. a kill inside ensure_venv_exists's rmtree->recreate window),
        # jam-update exec-failed every boot and nothing could ever recreate
        # it -- a permanent brick. This script runs on the SYSTEM python, so
        # it is the one thing that can break that loop: recreate the venv
        # here (same flags as jam-update's ensure_venv_exists), then proceed
        # to the normal pip repair below. Creation is offline-safe.
        log("venv python/pip missing -- recreating venv on system python")
        if not recreate_venv():
            log("venv recreation failed -- deferring to next boot")
            return 0
    if not wait_for_package_index():
        log("package index unreachable within bounded wait -- deferring repair to next boot/connectivity")
        return 0  # never block boot indefinitely; retry next cycle
    ok = repair()
    log("self-heal succeeded" if ok else "self-heal FAILED (will retry next boot)")
    return 0  # always exit 0: never wedge the boot sequence


def main():
    # Structural guarantee of the "never wedge boot" invariant: whatever goes
    # wrong (an OSError from a bad mount, a permission fault, anything), we log
    # it and exit 0 so the ordered-after jam services still start and the repair
    # simply retries next boot. This must NOT depend on every callee's internal
    # error handling staying perfect as the file evolves.
    try:
        return _attempt_self_heal()
    except Exception as e:
        log(f"unexpected error during self-heal -- deferring to next boot: {e}")
        return 0


if __name__ == '__main__':
    sys.exit(main())
