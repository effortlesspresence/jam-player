#!/usr/bin/env python3
"""
jam-installed-version-reporter.service

Oneshot systemd service. Fires on every boot once network is online and
reports the currently-installed code commit hash to the backend via
POST /jam-players/installed-version.

Why per-boot in addition to "after jam-update":
    - Devices that haven't updated recently still need their version
      reported (offline-playback devices that finally come online,
      devices whose jam-update has been failing, devices that were
      rolled back manually via SSH, etc.)
    - The post-update report can fail (transient network blip) and
      have no recovery path; the per-boot loop catches those.
    - We pay basically nothing for the redundancy: one HTTP call per
      boot, ~200 bytes payload.

Why oneshot:
    - We don't need a long-running daemon; a single best-effort POST
      with retries is enough.
    - systemd's `Restart=on-failure` provides reasonable retry
      semantics if the backend is briefly unreachable.

Why this lives in services_v2 (alongside other jam-* services):
    - Reuses common/api.py for Ed25519-signed requests
    - Reuses common/installed_version.py helpers
    - Matches the deployment pattern -- jam-update copies everything in
      services_v2/ to /opt/jam/services/.
"""
import sys
import time

from common.installed_version import (
    read_installed_version,
    report_installed_version_to_backend,
)
from common.logging_config import setup_service_logging

logger = setup_service_logging("jam-installed-version-reporter")


# Retry policy: try a small fixed number of times with backoff. The
# service unit also has `Restart=on-failure RestartSec=30` so even if
# all retries here fail, systemd retries the whole script another few
# times after that.
MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SEC = 5


def main() -> int:
    logger.info("=" * 60)
    logger.info("JAM Installed Version Reporter - Starting")
    logger.info("=" * 60)

    version = read_installed_version()
    if not version:
        # No version file yet. This is normal on the very first boot of
        # a fresh JP before jam-update has ever run. Exit success so
        # systemd doesn't keep retrying -- the post-update path in
        # jam_update.py will report once it has data.
        logger.info(
            "No /etc/jam/version.txt yet -- nothing to report. "
            "jam-update.py will report on first install."
        )
        return 0

    logger.info(f"Reporting installed version {version}")

    backoff = INITIAL_BACKOFF_SEC
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if report_installed_version_to_backend(version=version):
            logger.info(f"Successfully reported on attempt {attempt}")
            return 0

        if attempt < MAX_ATTEMPTS:
            logger.warning(
                f"Attempt {attempt}/{MAX_ATTEMPTS} failed -- "
                f"retrying in {backoff}s"
            )
            time.sleep(backoff)
            backoff *= 2  # exponential: 5, 10, 20, 40s
        else:
            logger.error(f"All {MAX_ATTEMPTS} attempts failed")

    # Exit non-zero so systemd's `Restart=on-failure` policy kicks in
    # for one more pass after RestartSec. After StartLimitBurst is hit,
    # systemd stops retrying -- the next boot will start fresh.
    return 1


if __name__ == "__main__":
    sys.exit(main())
