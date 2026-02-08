#!/usr/bin/env python3
"""
JAM Player Registration Status Poller

This service polls the JAM 2.0 API to check if the device has been registered.
It runs periodically (triggered by jam-registration-poller.timer) until the device
is registered, then creates the .registered flag file.

Flow:
1. Read device UUID from disk
2. Call GET /jam-players/{deviceUuid}/registration-status
3. If status is REGISTERED, create .registered flag and start jam-heartbeat
4. Exit (timer will trigger next run if flag doesn't exist)
"""

import sys
import os
from typing import Optional

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    get_device_uuid,
    is_device_registered,
    set_device_registered,
)
from common.api import get_api_base_url, api_request
from common.paths import REGISTERED_FLAG
from common.system import manage_service

logger = setup_service_logging('jam-registration-poller')


def check_registration_status(device_uuid: str) -> Optional[str]:
    """
    Check registration status from the JAM 2.0 API.

    Args:
        device_uuid: The device's UUID

    Returns:
        Registration status string ('ANNOUNCED', 'REGISTERED') or None on error
    """
    path = f"/jam-players/{device_uuid}/registration-status"
    logger.info(f"Checking registration status at {get_api_base_url()}{path}")

    # Use unsigned request - this endpoint has no authorizer
    response = api_request(
        method='GET',
        path=path,
        signed=False
    )

    if response is None:
        logger.error("No response from API (network error or timeout)")
        return None

    if response.status_code == 200:
        try:
            data = response.json()
            status = data.get('registrationStatus', {})
            # Handle EnumWithLabel format: {"value": "REGISTERED", "label": "Registered"}
            if isinstance(status, dict):
                return status.get('value')
            return status
        except Exception as e:
            logger.error(f"Error parsing response: {e}")
            return None
    elif response.status_code == 404:
        logger.warning("Device not found in backend - may not be announced yet")
        return None
    else:
        logger.error(f"API returned status {response.status_code}: {response.text}")
        return None


def main():
    log_service_start(logger, 'JAM Registration Poller')

    # Check if already registered (shouldn't happen due to ConditionPathExists, but be safe)
    if is_device_registered():
        logger.info("Device already registered - exiting")
        sys.exit(0)

    # Get device UUID
    device_uuid = get_device_uuid()
    if not device_uuid:
        logger.error("No device UUID found - cannot check registration status")
        sys.exit(1)

    logger.info(f"Device UUID: {device_uuid}")

    # Check registration status
    status = check_registration_status(device_uuid)

    if status is None:
        logger.warning("Could not determine registration status - will retry later")
        sys.exit(0)  # Exit cleanly, timer will retry

    logger.info(f"Registration status: {status}")

    if status == 'REGISTERED':
        logger.info("Device is REGISTERED - creating flag file")
        if set_device_registered():
            logger.info(f"Created {REGISTERED_FLAG}")
            logger.info("Registration complete!")

            # Ensure jam-heartbeat.service is running now that registration is complete.
            # Without this, heartbeat won't start until next reboot because
            # its ConditionPathExists was evaluated at boot when .registered
            # didn't exist yet.
            manage_service('jam-heartbeat.service', should_run=True)
        else:
            logger.error("Failed to create registered flag file")
            sys.exit(1)
    else:
        logger.info(f"Device is {status} - waiting for user to complete registration")

    sys.exit(0)


if __name__ == '__main__':
    main()
