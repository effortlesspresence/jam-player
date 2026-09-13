"""
JAM Player 2.0 - Shared Path Constants

All file and directory paths used by JP 2.0 services are defined here.
This ensures consistency across all services and makes updates easier.

Directory structure:
  /etc/jam/
    ├── config/              # Configuration files
    │   └── environment      # Optional: 'testing', 'staging', or 'prod' (defaults to prod)
    ├── credentials/         # Sensitive files (root-only, 0700)
    │   ├── api_signing_private_key
    │   ├── api_signing_public_key
    │   ├── ssh_private_key
    │   └── ssh_public_key
    └── device_data/         # Non-sensitive device data
        ├── device_uuid.txt
        ├── jp_image_id.txt  # Baked in during manufacturing
        ├── screen_id.txt    # Screen this device is linked to
        ├── location_timezone.txt  # IANA timezone from Location
        ├── display_orientation.txt  # LANDSCAPE, PORTRAIT_BOTTOM_ON_LEFT, or PORTRAIT_BOTTOM_ON_RIGHT
        ├── .first_boot_complete
        ├── .announced       # Created when announce-jp API succeeds
        ├── .registered      # Created when device is registered
        └── .internet_verified  # Maintained by jam-ble-state-manager

  /opt/jam/
    ├── venv/                # Python virtual environment
    ├── services/            # Symlinked from jam-player repo
    └── content/             # Downloaded content for display
        └── media/
            └── loop.mp4     # Main stitched content video
"""

import os
from pathlib import Path

# Base directories
JAM_ETC_DIR = Path('/etc/jam')

# Volatile state: tmpfs, so writing here never touches the SD card and
# nothing survives a reboot (which is exactly what we want for "is something
# happening right now" markers).
JAM_RUN_DIR = Path('/run/jam')

# Set by jam-ble-provisioning while a WiFi connection attempt started over
# BLE is still running, and cleared when it finishes. jam-ble-state-manager
# refuses to close the post-boot BLE recovery window while this exists, so a
# setup session started at minute 14 is never cut off mid-connect.
BLE_SESSION_ACTIVE_FLAG = JAM_RUN_DIR / 'ble_session_active'

# Touched by jam-ble-state-manager on every periodic tick (15 s) (and at the start
# of its boot-time check). The .internet_verified flag is a CACHE maintained
# by that one process; readers use this stamp's age to know whether the
# cache is being maintained at all. A stale stamp means "unknown", and every
# reader has an explicit fail direction for unknown -- the oneshot gates run,
# the display and BLE report offline. tmpfs: no SD-card write.
STATE_MANAGER_ALIVE_FLAG = JAM_RUN_DIR / 'state_manager_alive'
# mtime = the last time a REAL, signed API call succeeded (heartbeat/announce).
# tmpfs, so a reboot starts with no evidence. Read via network.api_recently_ok().
API_LAST_OK_FLAG = JAM_RUN_DIR / 'api_last_ok'


def touch_volatile_flag(path: Path) -> bool:
    """
    Create or refresh a marker under /run (tmpfs): mkdir parents, touch.

    Deliberately NOT safe_touch(): that one fsyncs because it targets the SD
    card, and there is nothing to sync on tmpfs. Never raises -- a marker is
    advisory and its absence has a defined meaning for every reader -- so
    callers just call it and move on.

    Returns:
        True if the marker exists afterwards, False if it could not be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return True
    except Exception:
        return False
DEVICE_DATA_DIR = JAM_ETC_DIR / 'device_data'
CREDENTIALS_DIR = JAM_ETC_DIR / 'credentials'
CONFIG_DIR = JAM_ETC_DIR / 'config'

# Optional log level override (e.g. "DEBUG" for troubleshooting). Read
# once at service startup by common.logging_config.setup_service_logging.
# Missing / empty / invalid -> INFO. Changes require service restart.
LOG_LEVEL_FILE = CONFIG_DIR / 'log_level'

# Device identification
DEVICE_UUID_FILE = DEVICE_DATA_DIR / 'device_uuid.txt'
JP_IMAGE_ID_FILE = DEVICE_DATA_DIR / 'jp_image_id.txt'  # Baked in during manufacturing

# Service flags
FIRST_BOOT_COMPLETE_FLAG = DEVICE_DATA_DIR / '.first_boot_complete'
BOOT_ERROR_FILE = JAM_ETC_DIR / 'boot_error.txt'

# Registration status flags (see design doc for ANNOUNCED vs REGISTERED states)
# .announced - created when jam-announce.service successfully calls announce-jp API
ANNOUNCED_FLAG = DEVICE_DATA_DIR / '.announced'
# .registered - created when jam-registration-poller sees REGISTERED status from API
REGISTERED_FLAG = DEVICE_DATA_DIR / '.registered'

# Connectivity status flag
# .internet_verified - maintained by jam-ble-state-manager
# Created when actual internet connectivity is verified, deleted when connectivity is lost.
# Used by jam-ble-provisioning for fast BLE reads (checking file exists vs slow HTTP check).
INTERNET_VERIFIED_FLAG = DEVICE_DATA_DIR / '.internet_verified'

# Screen linking - stores the ID of the screen this JP is linked to
# Written by jam-heartbeat.service when screenId changes in heartbeat response
SCREEN_ID_FILE = DEVICE_DATA_DIR / 'screen_id.txt'

# Location timezone - stores the IANA timezone from the Location this JP belongs to
# Written by jam-heartbeat.service when locationTimezone changes in heartbeat response
# The system timezone is then set via timedatectl to match this value
LOCATION_TIMEZONE_FILE = DEVICE_DATA_DIR / 'location_timezone.txt'

# Display orientation - stores the display orientation (LANDSCAPE, PORTRAIT_BOTTOM_ON_LEFT, PORTRAIT_BOTTOM_ON_RIGHT)
# Written by jam-ws-commands.service when SET_ORIENTATION command is received
# Also updated by jam-heartbeat.service as fallback
DISPLAY_ORIENTATION_FILE = DEVICE_DATA_DIR / 'display_orientation.txt'

# Outlet (Location) operational status as computed by the backend.
# Stored as JSON in EnumWithLabel shape:
#   {"value": "OFF_SEASON", "label": "Seasonal (Closed)"}
# The value is one of OPERATIONAL / DEACTIVATED / OFF_SEASON / SCHEDULED
# / PERIOD_ENDED. The label is the human-readable display string from
# the backend, used directly by jam-player-display in the inactive
# screen copy. Absent if the device has never received a value (older
# fielded builds, never-online devices, devices not yet linked to a
# location). Written by jam-heartbeat, jam-ws-commands (on
# SET_OUTLET_OPERATIONAL_STATUS), and jam-outlet-status-poller.
OUTLET_OPERATIONAL_STATUS_FILE = DEVICE_DATA_DIR / 'outlet_operational_status.json'

# Outlet (Location) display name, cached so the device can show
# "Outlet '<name>' is inactive" without a follow-up request. Same writers
# as OUTLET_OPERATIONAL_STATUS_FILE.
OUTLET_NAME_FILE = DEVICE_DATA_DIR / 'outlet_name.txt'

# Per-device randomized nightly reboot time, "HH:MM" format (local tz).
# Picked once on first jam-update run after the new code is installed,
# persisted forever after so the reboot time stays stable. Spreads
# fleet-wide post-reboot API call surges across a 2.5-hour window
# (01:45-04:15) instead of all hitting at 03:00 simultaneously.
# Delete this file to force re-randomization on the next jam-update.
NIGHTLY_REBOOT_TIME_FILE = DEVICE_DATA_DIR / 'nightly_reboot_time.txt'

# API signing keys (Ed25519)
API_SIGNING_PRIVATE_KEY_FILE = CREDENTIALS_DIR / 'api_signing_private_key'
API_SIGNING_PUBLIC_KEY_FILE = CREDENTIALS_DIR / 'api_signing_public_key'

# SSH keys (Ed25519)
SSH_PRIVATE_KEY_FILE = CREDENTIALS_DIR / 'ssh_private_key'
SSH_PUBLIC_KEY_FILE = CREDENTIALS_DIR / 'ssh_public_key'

# Configuration files
# Environment override: create this file with 'testing', 'staging', or 'prod'
# If not present, defaults to 'prod'
ENVIRONMENT_FILE = CONFIG_DIR / 'environment'

# All credential files that must exist for a properly provisioned device
REQUIRED_CREDENTIAL_FILES = [
    (DEVICE_UUID_FILE, "Device UUID"),
    (API_SIGNING_PRIVATE_KEY_FILE, "API signing private key"),
    (API_SIGNING_PUBLIC_KEY_FILE, "API signing public key"),
    (SSH_PRIVATE_KEY_FILE, "SSH private key"),
    (SSH_PUBLIC_KEY_FILE, "SSH public key"),
]

# =============================================================================
# Content directories (downloaded media for display)
# =============================================================================

OPT_JAM_DIR = Path('/opt/jam')
CONTENT_DIR = OPT_JAM_DIR / 'content'
MEDIA_DIR = CONTENT_DIR / 'media'
LOOP_VIDEO_PATH = MEDIA_DIR / 'loop.mp4'

# Legacy content paths (for backwards compatibility during migration)
# TODO: Remove these once all devices are migrated to JAM 2.0
LEGACY_HOME_DIR = Path('/home/comitup')
LEGACY_JAM_DIR = LEGACY_HOME_DIR / '.jam'
LEGACY_APP_DATA_DIR = LEGACY_JAM_DIR / 'app_data'
LEGACY_MEDIA_DIR = LEGACY_APP_DATA_DIR / 'live_media'
LEGACY_SCENES_DIR = LEGACY_APP_DATA_DIR / 'live_scenes'
LEGACY_LOOP_VIDEO_PATH = LEGACY_MEDIA_DIR / 'loop.mp4'


# =============================================================================
# File writing utilities
# =============================================================================

def safe_write_text(path: Path, content: str, mode: int = 0o644):
    """
    Write text to file and ensure it's flushed to disk immediately.

    This prevents data loss if power is cut shortly after writing.
    Critical for manufacturing QA where devices are power-cycled quickly.

    Args:
        path: Path to write to
        content: Text content to write
        mode: File permissions (default 0o644)
    """
    with open(path, 'w') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, mode)


def safe_touch(path: Path, mode: int = 0o644):
    """
    Create an empty file and ensure it's flushed to disk immediately.

    Use this instead of Path.touch() for flag files that must persist
    even if power is cut immediately after.

    Args:
        path: Path to create
        mode: File permissions (default 0o644)
    """
    safe_write_text(path, '', mode)


def safe_copy(src: Path, dest: Path, mode: int = 0o644):
    """
    Copy a file with explicit fsync so the data hits the SD card before
    we move on. Returns the number of bytes written.

    Use this instead of shutil.copy2() for any file that must survive a
    sudden reboot or power loss. We've seen JPs end up with 0-byte
    destination files after using shutil.copy2() then power-cycling --
    shutil writes the data but doesn't fsync, so the directory entry's
    size is committed while the actual data blocks may not be, and ext4
    journal recovery can then truncate the file back to 0.

    Raises:
        OSError: If the copy fails or the post-copy size doesn't match
            the source size (defensive integrity check).
    """
    src_bytes = src.read_bytes()
    src_size = len(src_bytes)

    # Write to a sibling temp file, fsync, then atomically rename over the
    # destination. The old in-place open(dest,'wb') TRUNCATED the live file
    # first, so a kill between truncate and write-complete left a 0-byte/
    # partial destination (fatal when the destination is e.g. a systemd unit
    # or a service script). With rename, the destination is at every instant
    # either the complete old file or the complete new file.
    # PID-suffixed so two processes safe_copying the same dest can never
    # truncate each other's in-flight temp file. (Today jam_update is the
    # sole caller and is serialized by systemd, but this module is shared.)
    tmp = dest.with_name(f"{dest.name}.safecopy-tmp.{os.getpid()}")
    try:
        with open(tmp, 'wb') as f:
            f.write(src_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.rename(tmp, dest)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # fsync the parent directory so the rename itself survives power loss
    # (otherwise ext4 journal recovery can resurrect the old directory entry).
    try:
        dir_fd = os.open(str(dest.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # best-effort: file data is already fsynced

    # Defensive: re-stat and verify size. If a filesystem layer truncated
    # us silently, fail loudly so the caller can retry or report.
    dest_size = dest.stat().st_size
    if dest_size != src_size:
        raise OSError(
            f"safe_copy size mismatch: src={src_size} bytes, "
            f"dest={dest_size} bytes (path: {dest})"
        )

    return src_size
