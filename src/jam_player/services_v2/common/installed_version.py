"""
Single source of truth for the locally-installed jam-player commit hash.

Owns the lifecycle of /etc/jam/version.txt -- the canonical "what code
is currently installed" marker. Read and write happen through this
module so the path, format, and durability semantics live in one place.
Also owns the backend report side: posting the installed commit to
POST /jam-players/installed-version.

Callers:
  - jam_update.py -- writes the file via write_installed_version() at
    the end of every successful install, then calls
    report_installed_version_to_backend() so the backend knows
    immediately.
  - jam-installed-version-reporter.service -- oneshot systemd service
    that fires on every boot once the network is online, calls
    report_installed_version_to_backend() so we keep the backend in
    sync for devices that haven't updated recently or whose post-update
    report failed transiently.
  - jam_update.py's backup/restore flow uses VERSION_FILE directly for
    the rollback path.
  - common/display_cache.py reads VERSION_FILE to key the display
    cache so old commits' cache entries get cleaned up on upgrade.

Format: a single line containing the full 40-char git commit hash of
HEAD on the jam-player branch the device tracks. No trailing whitespace
beyond the strip. Format may evolve (e.g. semver) -- callers that read
should treat the value as an opaque non-empty string.

Durability: write_installed_version() does write + flush + fsync via
common.paths.safe_write_text so a power-cycle right after install can't
leave us with a 0-byte version.txt that would lie to the rest of the
fleet's tooling.
"""
import logging
from pathlib import Path
from typing import Optional

from common.api import api_request

logger = logging.getLogger(__name__)


# Canonical path for the "currently installed commit" marker. All other
# modules that need this path should import VERSION_FILE from here
# rather than re-defining it -- a single source of truth keeps file path
# changes safe.
VERSION_FILE = Path('/etc/jam/version.txt')


def read_installed_version() -> Optional[str]:
    """
    Return the installed code commit hash from VERSION_FILE.

    Returns None on early boot before jam-update has ever run, or if the
    file is unreadable for any reason (permissions, fs error, etc.).
    Callers should not pass None to the backend reporter -- the endpoint
    requires a non-empty installedVersion field.
    """
    try:
        commit = VERSION_FILE.read_text().strip()
        return commit if commit else None
    except OSError:
        return None


def write_installed_version(version: str) -> bool:
    """
    Persist `version` to VERSION_FILE with fsync.

    Called by jam_update.py at the end of every successful install. The
    fsync (via safe_write_text) is critical: a power-cycle right after
    we return must not leave behind a 0-byte version.txt, because that
    would make subsequent boots think the device has no version
    installed and skew our backend telemetry / break display-cache
    invalidation.

    Args:
        version: The full commit hash (or future-format identifier) to
            persist. Must be non-empty -- empty values are rejected
            because they'd be indistinguishable from "file missing" to
            read_installed_version().

    Returns:
        True on success, False on any write error.
    """
    if not version or not version.strip():
        logger.error("Refusing to write empty installed version")
        return False

    try:
        # Local import to avoid import errors during jam_update.py's
        # self-re-execution path: the new code's paths.py may not exist
        # in the venv at the moment this module first imports.
        from common.paths import safe_write_text

        VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(VERSION_FILE, version)
        logger.info(f"Installed version written: {version[:12]}...")
        return True
    except Exception as e:
        logger.error(f"Failed to write installed version: {e}")
        return False


def report_installed_version_to_backend(version: Optional[str] = None) -> bool:
    """
    POST /jam-players/installed-version with the given (or current)
    commit hash.

    Args:
        version: Override the commit hash to report. If None, reads it
            from VERSION_FILE.

    Returns:
        True if the backend accepted the report. False on any failure
        (HTTP error, file unreadable, network down, etc.). Callers
        should retry on False -- this function does not retry internally.
    """
    if version is None:
        version = read_installed_version()

    if not version:
        logger.warning(
            "Cannot report installed version: /etc/jam/version.txt is "
            "missing or empty"
        )
        return False

    response = api_request(
        method='POST',
        path='/jam-players/installed-version',
        body={'installedVersion': version},
        signed=True,
    )

    if response is None:
        logger.warning("Failed to report installed version: no response")
        return False

    if response.status_code == 200:
        logger.info(f"Reported installed version {version} to backend")
        return True

    logger.warning(
        f"Backend rejected installed-version report "
        f"(status={response.status_code}): {response.text[:200]}"
    )
    return False
