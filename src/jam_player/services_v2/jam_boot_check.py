#!/usr/bin/env python3
"""
JAM Boot Check Service

This service runs once on every boot after jam-first-boot.service completes.
It validates the system state and ensures the device is ready to operate.

Checks performed:
0. Clear network impairments (from jam-simulate-network testing tool)
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
import subprocess
from common.system import (
    check_chrony_sync,
    check_required_services,
    manage_service,
    clear_network_impairments,
)

logger = setup_service_logging('jam-boot-check')


def ensure_system_dependencies() -> bool:
    """
    Ensure required system packages are installed.

    This handles the case where JAM 1.0 first-batch devices (JPB_3_14_24)
    migrated to 2.0 but the initial package installation failed.

    Returns True if all dependencies are satisfied.
    """
    all_installed = True

    # Check MPV (required for video playback)
    result = subprocess.run(['which', 'mpv'], capture_output=True)
    if result.returncode != 0:
        logger.info("MPV not installed, installing...")
        try:
            # Update apt cache first
            subprocess.run(['sudo', 'apt-get', 'update'], timeout=120, check=False)
            result = subprocess.run(
                ['sudo', 'apt-get', 'install', '-y', 'mpv'],
                timeout=300,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                logger.info("MPV installed successfully")
            else:
                logger.error(f"Failed to install MPV: {result.stderr}")
                all_installed = False
        except subprocess.TimeoutExpired:
            logger.error("Timeout installing MPV")
            all_installed = False
        except Exception as e:
            logger.error(f"Error installing MPV: {e}")
            all_installed = False

    # Check Tailscale (required for remote access)
    result = subprocess.run(['which', 'tailscale'], capture_output=True)
    if result.returncode != 0:
        logger.info("Tailscale not installed, installing...")
        try:
            result = subprocess.run(
                ['sh', '-c', 'curl -fsSL https://tailscale.com/install.sh | sh'],
                timeout=300,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                logger.info("Tailscale installed successfully")
            else:
                logger.error(f"Failed to install Tailscale: {result.stderr}")
                all_installed = False
        except subprocess.TimeoutExpired:
            logger.error("Timeout installing Tailscale")
            all_installed = False
        except Exception as e:
            logger.error(f"Error installing Tailscale: {e}")
            all_installed = False

    return all_installed

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

    # 0. Clear any network impairments left from testing
    # This ensures jam-simulate-network settings don't persist across reboots
    # or get baked into device images
    sd_notifier.notify("STATUS=Clearing network impairments...")
    clear_network_impairments()

    device_uuid = get_device_uuid()
    if device_uuid:
        logger.info(f"Device UUID: {device_uuid}")
    else:
        logger.warning("Device UUID not found")

    # 1. Wait briefly for network connectivity
    sd_notifier.notify("STATUS=Checking network connectivity...")
    network_connected, conn_type = wait_for_network(timeout_seconds=NETWORK_WAIT_TIMEOUT_SECONDS)

    # 1b. Ensure system dependencies are installed (requires network)
    # This handles first-batch JAM 1.0 devices that migrated without MPV/Tailscale
    if network_connected:
        sd_notifier.notify("STATUS=Checking system dependencies...")
        deps_ok = ensure_system_dependencies()
        if not deps_ok:
            logger.warning("Some system dependencies could not be installed - will retry on next boot")

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
