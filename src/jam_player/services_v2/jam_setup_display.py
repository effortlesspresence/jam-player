#!/usr/bin/env python3
"""
JAM Player Setup Display Service

This service runs at boot and:
1. Checks if the device is provisioned with the backend
2. If YES: Exits immediately (allows jam-player-display to start)
3. If NO: Shows the welcome/setup screen with QR code
4. Monitors for provisioning completion
5. Once provisioned: Kills setup screen and exits (allows jam-player-display to start)

This keeps the main jam-player-display service "dumb" and stable.
"""

import sys
import os
import time
import signal
import subprocess

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import is_device_registered, get_device_uuid
from common.system import get_systemd_notifier, setup_signal_handlers

logger = setup_service_logging('jam-setup-display')

# systemd notifier
sd_notifier = get_systemd_notifier()

# Polling interval for checking provisioning status (seconds)
POLL_INTERVAL = 5

# Global for display process
display_process = None


def show_setup_screen():
    """Launch the setup screen display."""
    global display_process

    device_uuid = get_device_uuid() or 'unknown'
    logger.info(f"Showing setup screen for device: {device_uuid}")

    # Kill any existing display process
    kill_setup_screen()

    try:
        # Launch display_setup.py
        script_path = os.path.join(os.path.dirname(__file__), 'display_setup.py')
        display_process = subprocess.Popen(
            [sys.executable, script_path, '--uuid', device_uuid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
        logger.info(f"Setup screen process started: PID {display_process.pid}")
    except Exception as e:
        logger.error(f"Failed to launch setup screen: {e}")


def kill_setup_screen():
    """Kill the setup screen display process."""
    global display_process

    if display_process:
        try:
            display_process.terminate()
            display_process.wait(timeout=5)
            logger.info("Setup screen process terminated")
        except Exception as e:
            logger.warning(f"Error terminating setup screen: {e}")
            try:
                display_process.kill()
            except:
                pass
        display_process = None

    # Also kill any feh processes showing our image
    try:
        subprocess.run(
            ['pkill', '-f', 'feh.*jam_setup.png'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except:
        pass


def wait_for_provisioning():
    """
    Wait for the device to be provisioned.
    Returns True when provisioned, False on shutdown signal.
    """
    shutdown_requested = False

    def handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        logger.info(f"Received signal {signum}, shutting down...")
        shutdown_requested = True

    # Setup signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    logger.info("Waiting for device to be provisioned...")
    sd_notifier.notify("STATUS=Waiting for provisioning")

    while not shutdown_requested:
        if is_device_registered():
            logger.info("Device is now provisioned!")
            return True

        # Send watchdog ping if configured
        sd_notifier.notify("WATCHDOG=1")

        time.sleep(POLL_INTERVAL)

    return False


def main():
    log_service_start(logger, 'JAM Setup Display Service')

    # Check if already provisioned
    if is_device_registered():
        logger.info("Device is already provisioned - exiting to allow player display to start")
        sd_notifier.notify("STATUS=Already provisioned")
        sd_notifier.notify("READY=1")
        sys.exit(0)

    # Not provisioned - show setup screen
    logger.info("Device not provisioned - showing setup screen")
    sd_notifier.notify("STATUS=Showing setup screen")
    sd_notifier.notify("READY=1")

    show_setup_screen()

    # Wait for provisioning to complete
    provisioned = wait_for_provisioning()

    # Cleanup
    kill_setup_screen()

    if provisioned:
        logger.info("Provisioning complete - exiting to allow player display to start")
        sd_notifier.notify("STATUS=Provisioning complete")
        sys.exit(0)
    else:
        logger.info("Shutdown requested before provisioning complete")
        sys.exit(0)


if __name__ == '__main__':
    main()
