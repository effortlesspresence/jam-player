#!/usr/bin/env python3
"""
JAM Player Announce Service

This service announces the device's presence to the JAM 2.0 backend API.
It runs once when the device first gets internet connectivity (if not already announced).

Flow:
1. Read device credentials from disk (UUID, public keys, JP image ID)
2. Call POST /jam-players/announce
3. On success, create .announced flag file
4. Exit

The .announced flag prevents this from running again on subsequent boots.
"""

import sys
import os

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    get_device_uuid,
    get_api_signing_public_key,
    get_ssh_public_key,
    get_jp_image_id,
    is_device_announced,
    set_device_announced,
)
from common.api import get_api_base_url, api_request
from common.paths import ANNOUNCED_FLAG
from common.system import start_service

logger = setup_service_logging('jam-announce')


def announce_to_backend(
    device_uuid: str,
    api_signing_public_key: str,
    ssh_public_key: str,
    jp_image_id: str
) -> bool:
    """
    Announce this device to the JAM 2.0 backend.

    Args:
        device_uuid: The device's unique identifier
        api_signing_public_key: The device's API signing public key (base64)
        ssh_public_key: The device's SSH public key
        jp_image_id: The JP image ID baked into this device

    Returns:
        True if announcement succeeded, False otherwise
    """
    payload = {
        'deviceUuid': device_uuid,
        'apiSigningPublicKey': api_signing_public_key,
        'sshPublicKey': ssh_public_key,
        'jpImageId': jp_image_id,
    }

    logger.info(f"Announcing device to {get_api_base_url()}/jam-players/announce")
    logger.info(f"  deviceUuid: {device_uuid}")
    logger.info(f"  jpImageId: {jp_image_id}")

    # Use unsigned request - announce-jp has no authorizer
    response = api_request(
        method='POST',
        path='/jam-players/announce',
        body=payload,
        signed=False
    )

    if response is None:
        logger.error("No response from API (network error or timeout)")
        return False

    if response.status_code == 200:
        logger.info("Announcement successful!")
        return True
    elif response.status_code == 409:
        # Already exists - this is fine, treat as success
        logger.info("Device already announced/registered - treating as success")
        return True
    else:
        logger.error(f"API returned status {response.status_code}: {response.text}")
        return False


def main():
    log_service_start(logger, 'JAM Announce Service')

    # Check if already announced (shouldn't happen due to ConditionPathExists, but be safe)
    if is_device_announced():
        logger.info("Device already announced - exiting")
        sys.exit(0)

    # Gather required credentials
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.error("No device UUID found - jam-first-boot may not have completed")
        sys.exit(1)

    api_signing_public_key = get_api_signing_public_key()
    if not api_signing_public_key:
        logger.error("No API signing public key found")
        sys.exit(1)

    ssh_public_key = get_ssh_public_key()
    if not ssh_public_key:
        logger.error("No SSH public key found")
        sys.exit(1)

    jp_image_id = get_jp_image_id()
    if not jp_image_id:
        logger.error("No JP image ID found - this should be baked into the image")
        sys.exit(1)

    logger.info(f"Device UUID: {device_uuid}")
    logger.info(f"JP Image ID: {jp_image_id}")

    # Announce to backend
    if announce_to_backend(device_uuid, api_signing_public_key, ssh_public_key, jp_image_id):
        logger.info("Creating announced flag file")
        if set_device_announced():
            logger.info(f"Created {ANNOUNCED_FLAG}")
            logger.info("Announcement complete!")

            # Start jam-tailscale.service now that we're announced
            # On first boot, jam-tailscale.service may have already run and exited
            # because the device wasn't announced yet. Now that we're announced,
            # we can set up Tailscale for remote access.
            if not start_service('jam-tailscale.service'):
                # Not fatal - Tailscale can be set up on next boot
                logger.warning("jam-tailscale.service did not start - will retry on next boot")

            sys.exit(0)
        else:
            logger.error("Failed to create announced flag file")
            sys.exit(1)
    else:
        logger.error("Announcement failed - will retry on next boot or service restart")
        sys.exit(1)


if __name__ == '__main__':
    main()
