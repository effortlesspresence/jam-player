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
        ├── .first_boot_complete
        ├── .announced       # Created when announce-jp API succeeds
        ├── .registered      # Created when device is registered
        └── .internet_verified  # Maintained by jam-ble-state-manager
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
