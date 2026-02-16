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

from pathlib import Path

# Base directories
JAM_ETC_DIR = Path('/etc/jam')
DEVICE_DATA_DIR = JAM_ETC_DIR / 'device_data'
CREDENTIALS_DIR = JAM_ETC_DIR / 'credentials'
CONFIG_DIR = JAM_ETC_DIR / 'config'

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
