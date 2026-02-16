"""
JAM Player 2.0 - Credential Utilities

Functions for reading and validating device credentials.
All credential files are stored in /etc/jam/credentials with root-only access.
"""

import os
import logging
from pathlib import Path
from typing import Optional, Tuple

from .paths import (
    DEVICE_UUID_FILE,
    JP_IMAGE_ID_FILE,
    API_SIGNING_PRIVATE_KEY_FILE,
    API_SIGNING_PUBLIC_KEY_FILE,
    SSH_PUBLIC_KEY_FILE,
    REQUIRED_CREDENTIAL_FILES,
    FIRST_BOOT_COMPLETE_FLAG,
    ANNOUNCED_FLAG,
    REGISTERED_FLAG,
    SCREEN_ID_FILE,
    LOCATION_TIMEZONE_FILE,
    DISPLAY_ORIENTATION_FILE,
)

logger = logging.getLogger(__name__)


def get_device_uuid() -> Optional[str]:
    """
    Read the device UUID from file.

    Returns:
        The device UUID string, or None if not found/readable.
    """
    try:
        if DEVICE_UUID_FILE.exists():
            uuid = DEVICE_UUID_FILE.read_text().strip()
            if uuid:
                return uuid
            logger.warning("Device UUID file exists but is empty")
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {DEVICE_UUID_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading device UUID: {e}")
        return None


def get_device_uuid_short(length: int = 5) -> Optional[str]:
    """
    Get the last N characters of the device UUID.

    Used for BLE device naming (JAM-PLAYER-XXXXX).

    Args:
        length: Number of characters to return (default 5)

    Returns:
        Last N characters of UUID in uppercase, or None if unavailable.
    """
    uuid = get_device_uuid()
    if uuid:
        # Remove hyphens and get last N chars
        clean_uuid = uuid.replace('-', '')
        return clean_uuid[-length:].upper()
    return None


def get_jp_image_id() -> Optional[str]:
    """
    Read the JP Image ID from file.

    The JP Image ID is baked into the device during manufacturing and identifies
    which image release this device was built from.

    Returns:
        The JP Image ID string (e.g., "JAM-2025-01-A"), or None if not found/readable.
    """
    try:
        if JP_IMAGE_ID_FILE.exists():
            image_id = JP_IMAGE_ID_FILE.read_text().strip()
            if image_id:
                return image_id
            logger.warning("JP Image ID file exists but is empty")
        else:
            logger.warning(f"JP Image ID file not found: {JP_IMAGE_ID_FILE}")
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {JP_IMAGE_ID_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading JP Image ID: {e}")
        return None


def get_api_signing_private_key() -> Optional[str]:
    """
    Read the API signing private key (base64 encoded).

    Returns:
        The private key string, or None if not found/readable.
    """
    try:
        if API_SIGNING_PRIVATE_KEY_FILE.exists():
            key = API_SIGNING_PRIVATE_KEY_FILE.read_text().strip()
            if key:
                return key
            logger.warning("API signing private key file exists but is empty")
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {API_SIGNING_PRIVATE_KEY_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading API signing private key: {e}")
        return None


def get_api_signing_public_key() -> Optional[str]:
    """
    Read the API signing public key (base64 encoded).

    Returns:
        The public key string, or None if not found/readable.
    """
    try:
        if API_SIGNING_PUBLIC_KEY_FILE.exists():
            key = API_SIGNING_PUBLIC_KEY_FILE.read_text().strip()
            if key:
                return key
            logger.warning("API signing public key file exists but is empty")
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {API_SIGNING_PUBLIC_KEY_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading API signing public key: {e}")
        return None


def get_ssh_public_key() -> Optional[str]:
    """
    Read the SSH public key.

    Returns:
        The SSH public key string, or None if not found/readable.
    """
    try:
        if SSH_PUBLIC_KEY_FILE.exists():
            key = SSH_PUBLIC_KEY_FILE.read_text().strip()
            if key:
                return key
            logger.warning("SSH public key file exists but is empty")
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {SSH_PUBLIC_KEY_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading SSH public key: {e}")
        return None


def validate_credentials() -> Tuple[bool, str]:
    """
    Validate that all required credential files exist and are not empty.

    Returns:
        Tuple of (all_valid, error_message)
        If all_valid is True, error_message is empty.
        If all_valid is False, error_message describes what's missing.
    """
    missing = []
    empty = []

    for file_path, name in REQUIRED_CREDENTIAL_FILES:
        if not file_path.exists():
            missing.append(name)
            logger.error(f"Missing credential file: {name} at {file_path}")
        elif file_path.stat().st_size == 0:
            empty.append(name)
            logger.error(f"Empty credential file: {name} at {file_path}")

    if missing or empty:
        errors = []
        if missing:
            errors.append(f"Missing: {', '.join(missing)}")
        if empty:
            errors.append(f"Empty: {', '.join(empty)}")
        return False, "; ".join(errors)

    logger.info("All credential files validated successfully")
    return True, ""


def is_first_boot_complete() -> bool:
    """
    Check if the first boot service has completed successfully.

    Returns:
        True if first boot flag file exists.
    """
    return FIRST_BOOT_COMPLETE_FLAG.exists()


def is_device_announced() -> bool:
    """
    Check if the device has announced itself to the JAM backend.

    A device is ANNOUNCED when jam-announce.service successfully calls
    the announce-jp API endpoint.

    Returns:
        True if .announced flag file exists.
    """
    return ANNOUNCED_FLAG.exists()


def is_device_registered() -> bool:
    """
    Check if the device has been fully registered with the JAM backend.

    A device is REGISTERED when:
    1. A user completes registration via the JAM Setup mobile app
    2. jam-registration-poller.service detects REGISTERED status from API

    Returns:
        True if .registered flag file exists.
    """
    return REGISTERED_FLAG.exists()


def set_device_announced() -> bool:
    """
    Mark the device as announced to the backend.

    Called by jam-announce.service after successfully calling announce-jp API.

    Returns:
        True if flag was written successfully.
    """
    try:
        ANNOUNCED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        ANNOUNCED_FLAG.touch()
        logger.info("Device marked as announced")
        return True
    except Exception as e:
        logger.error(f"Error setting announced flag: {e}")
        return False


def set_device_registered() -> bool:
    """
    Mark the device as registered with the backend.

    Called by jam-registration-poller.service when it detects REGISTERED status.
    Also ensures the device is marked as announced, since a registered device
    is implicitly announced.

    Returns:
        True if flag was written successfully.
    """
    try:
        set_device_announced()  # Registered implies announced
        REGISTERED_FLAG.parent.mkdir(parents=True, exist_ok=True)
        REGISTERED_FLAG.touch()
        logger.info("Device marked as registered")
        return True
    except Exception as e:
        logger.error(f"Error setting registered flag: {e}")
        return False

def clear_registration_flags() -> bool:
    """
    Clear all registration flags.

    Used for factory reset or re-provisioning scenarios.

    Returns:
        True if flags were cleared successfully.
    """
    try:
        if ANNOUNCED_FLAG.exists():
            ANNOUNCED_FLAG.unlink()
            logger.info("Announced flag cleared")
        if REGISTERED_FLAG.exists():
            REGISTERED_FLAG.unlink()
            logger.info("Registered flag cleared")
        return True
    except Exception as e:
        logger.error(f"Error clearing registration flags: {e}")
        return False


def get_screen_id() -> Optional[str]:
    """
    Read the linked screen ID from file.

    The screen ID is the ID of the Screen this JAM Player is linked to.
    It's updated by jam-heartbeat.service whenever the backend reports a change.

    Returns:
        The screen ID string, or None if not linked/not found.
    """
    try:
        if SCREEN_ID_FILE.exists():
            screen_id = SCREEN_ID_FILE.read_text().strip()
            if screen_id:
                return screen_id
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {SCREEN_ID_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading screen ID: {e}")
        return None


def set_screen_id(screen_id: Optional[str]) -> bool:
    """
    Write the linked screen ID to file.

    Called by jam-heartbeat.service when the screenId in the heartbeat
    response differs from the current value.

    Args:
        screen_id: The screen ID to write, or None to clear

    Returns:
        True if successfully written.
    """
    try:
        SCREEN_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if screen_id:
            SCREEN_ID_FILE.write_text(screen_id.strip())
            logger.info(f"Screen ID set to: {screen_id}")
        else:
            # Clear the file if screen_id is None (device unlinked)
            if SCREEN_ID_FILE.exists():
                SCREEN_ID_FILE.unlink()
                logger.info("Screen ID cleared (device unlinked)")
        return True
    except Exception as e:
        logger.error(f"Error setting screen ID: {e}")
        return False


def update_screen_id_if_changed(new_screen_id: Optional[str]) -> bool:
    """
    Update the local screen_id.txt if the value has changed.

    Compares new_screen_id against the current file contents and updates
    only if different. Handles linking, unlinking, and screen changes.

    Args:
        new_screen_id: The screen ID from the backend, or None if unlinked

    Returns:
        True if the screen ID was changed, False if unchanged
    """
    current_screen_id = get_screen_id()

    if new_screen_id == current_screen_id:
        return False

    if new_screen_id:
        logger.info(f"Screen ID changed: {current_screen_id} -> {new_screen_id}")
    else:
        logger.info(f"Device unlinked from screen: {current_screen_id} -> None")

    if set_screen_id(new_screen_id):
        logger.info("Screen ID file updated successfully")
        return True
    else:
        logger.error("Failed to update screen ID file")
        return False


def get_location_timezone() -> Optional[str]:
    """
    Read the location timezone from file.

    The location timezone is the IANA timezone identifier (e.g., "America/New_York")
    from the Location this JAM Player belongs to. It's updated by jam-heartbeat.service
    whenever the backend reports a change.

    Returns:
        The IANA timezone string, or None if not set/not found.
    """
    try:
        if LOCATION_TIMEZONE_FILE.exists():
            timezone = LOCATION_TIMEZONE_FILE.read_text().strip()
            if timezone:
                return timezone
        return None
    except PermissionError:
        logger.error(f"Permission denied reading {LOCATION_TIMEZONE_FILE}")
        return None
    except Exception as e:
        logger.error(f"Error reading location timezone: {e}")
        return None


def set_location_timezone(timezone: Optional[str]) -> bool:
    """
    Write the location timezone to file.

    Called by jam-heartbeat.service when the locationTimezone in the heartbeat
    response differs from the current value.

    Args:
        timezone: The IANA timezone string to write, or None to clear

    Returns:
        True if successfully written.
    """
    try:
        LOCATION_TIMEZONE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if timezone:
            LOCATION_TIMEZONE_FILE.write_text(timezone.strip())
            logger.info(f"Location timezone set to: {timezone}")
        else:
            # Clear the file if timezone is None
            if LOCATION_TIMEZONE_FILE.exists():
                LOCATION_TIMEZONE_FILE.unlink()
                logger.info("Location timezone cleared")
        return True
    except Exception as e:
        logger.error(f"Error setting location timezone: {e}")
        return False


def update_timezone_if_changed(new_timezone: Optional[str]) -> bool:
    """
    Update the local location_timezone.txt if the value has changed.

    Compares new_timezone against the current file contents and updates
    only if different.

    Args:
        new_timezone: The IANA timezone from the backend, or None if not set

    Returns:
        True if the timezone was changed, False if unchanged
    """
    current_timezone = get_location_timezone()

    if new_timezone == current_timezone:
        return False

    logger.info(f"Location timezone changed: {current_timezone} -> {new_timezone}")

    if set_location_timezone(new_timezone):
        logger.info("Location timezone file updated successfully")
        return True
    else:
        logger.error("Failed to update location timezone file")
        return False


# =============================================================================
# Display Orientation Functions
# =============================================================================

def get_display_orientation() -> Optional[str]:
    """
    Read the display orientation from file.

    Returns:
        The orientation string (LANDSCAPE, PORTRAIT_BOTTOM_ON_LEFT,
        PORTRAIT_BOTTOM_ON_RIGHT), or None if not found/readable.
    """
    try:
        if DISPLAY_ORIENTATION_FILE.exists():
            orientation = DISPLAY_ORIENTATION_FILE.read_text().strip()
            return orientation if orientation else None
        return None
    except Exception as e:
        logger.error(f"Error reading display orientation: {e}")
        return None


def set_display_orientation(orientation: Optional[str]) -> bool:
    """
    Write the display orientation to file.

    Called by jam-heartbeat.service when the displayOrientation in the heartbeat
    response differs from the current value.

    Args:
        orientation: The orientation string to write, or None to use default

    Returns:
        True if successfully written.
    """
    try:
        DISPLAY_ORIENTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        if orientation:
            DISPLAY_ORIENTATION_FILE.write_text(orientation.strip())
            logger.info(f"Display orientation set to: {orientation}")
        else:
            # Write default if orientation is None
            DISPLAY_ORIENTATION_FILE.write_text('LANDSCAPE')
            logger.info("Display orientation set to default: LANDSCAPE")
        return True
    except Exception as e:
        logger.error(f"Error setting display orientation: {e}")
        return False


def update_orientation_if_changed(new_orientation: Optional[str]) -> bool:
    """
    Update the local display_orientation.txt if the value has changed.

    Compares new_orientation against the current file contents and updates
    only if different.

    Args:
        new_orientation: The orientation from the backend

    Returns:
        True if the orientation was changed, False if unchanged
    """
    current_orientation = get_display_orientation()

    # Treat None from backend as LANDSCAPE (the default)
    effective_new = new_orientation if new_orientation else 'LANDSCAPE'

    if effective_new == current_orientation:
        return False

    logger.info(f"Display orientation changed: {current_orientation} -> {effective_new}")

    if set_display_orientation(effective_new):
        logger.info("Display orientation file updated successfully")
        return True
    else:
        logger.error("Failed to update display orientation file")
        return False
