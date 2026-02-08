#!/usr/bin/env python3
"""
JAM Boot Check Service

This service runs once on every boot after jam-first-boot.service completes.
It validates the system state and ensures the device is ready to operate.

Checks performed:
1. Network connectivity (WiFi or Ethernet)
2. JAM 2.0 API availability (non-blocking)
3. System clock synchronization via chrony
4. Required system services are running

Note: Credential validation is NOT done here - we trust jam-first-boot.service.
"""

import sys
from pathlib import Path

# Add services directory to path for common module imports
sys.path.insert(0, str(Path(__file__).parent))

import sdnotify

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    get_device_uuid,
)
from common.network import (
    wait_for_network,
)
from common.api import (
    check_api_availability,
)
from common.system import (
    check_chrony_sync,
    check_required_services,
    manage_service,
)

logger = setup_service_logging('jam-boot-check')

# systemd notify
sd_notifier = sdnotify.SystemdNotifier()

# Service name constants
BLE_PROVISIONING_SERVICE = 'jam-ble-provisioning.service'

# Timeouts
NETWORK_WAIT_TIMEOUT_SECONDS = 30


def run_boot_check() -> bool:
    """
    Execute all boot check tasks.

    Returns True if boot check completed (even with warnings).
    """
    log_service_start(logger, 'JAM Boot Check Service')

    # Notify systemd we're starting
    sd_notifier.notify("STATUS=Running boot checks...")

    device_uuid = get_device_uuid()
    if device_uuid:
        logger.info(f"Device UUID: {device_uuid}")
    else:
        logger.warning("Device UUID not found")

    # 1. Wait briefly for network connectivity
    sd_notifier.notify("STATUS=Checking network connectivity...")
    network_connected, conn_type = wait_for_network(timeout_seconds=NETWORK_WAIT_TIMEOUT_SECONDS)

    # 2. Manage BLE provisioning service based on network state
    sd_notifier.notify("STATUS=Managing BLE provisioning...")

    if not network_connected:
        # No network - start BLE for WiFi setup
        logger.info("No network connectivity - starting BLE provisioning for WiFi setup")
        manage_service(BLE_PROVISIONING_SERVICE, should_run=True)
    else:
        # Network connected - stop BLE provisioning (if running)
        logger.info("Network connected - stopping BLE provisioning")
        manage_service(BLE_PROVISIONING_SERVICE, should_run=False)

    # 3. Check API availability (non-blocking - offline playback must work)
    if network_connected:
        sd_notifier.notify("STATUS=Checking API availability...")
        api_available = check_api_availability()
        if not api_available:
            logger.warning("JAM 2.0 API not available - device will operate in offline mode")
    else:
        logger.info("Skipping API check - no network connectivity")
        api_available = False

    # 4. Check chrony sync (non-blocking - for video wall sync)
    sd_notifier.notify("STATUS=Checking time sync...")
    clock_synced = check_chrony_sync()
    if not clock_synced:
        logger.warning("System clock not synchronized - video wall sync may be affected")

    # 5. Check required system services
    sd_notifier.notify("STATUS=Checking system services...")
    services_ok, failed_services = check_required_services()
    if not services_ok:
        logger.warning(f"Some system services not running: {', '.join(failed_services)}")

    # Final summary
    logger.info("=" * 60)
    logger.info("JAM Boot Check Completed")
    sd_notifier.notify("STATUS=Boot check complete")

    logger.info(f"  Network: {'Connected via ' + conn_type if network_connected else 'Not connected'}")
    logger.info(f"  API: {'Available' if api_available else 'Not available'}")
    logger.info(f"  Clock sync: {'Synced' if clock_synced else 'Not synced'}")
    logger.info(f"  System services: {'All OK' if services_ok else f'Failed: {failed_services}'}")
    logger.info("=" * 60)

    return True


def main():
    """Entry point for the service."""
    try:
        success = run_boot_check()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unhandled exception in boot check service: {e}")
        sd_notifier.notify(f"STATUS=FAILED: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
