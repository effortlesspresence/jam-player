#!/usr/bin/env python3
"""
JAM Player Heartbeat Service

This service sends periodic heartbeats to the JAM 2.0 backend API.
It runs continuously, sending a heartbeat every 5 minutes.

Purpose:
1. Update lastSeenAt on the backend so the JAM Player shows as "online"
2. Receive screenId from backend and update local screen_id.txt if changed
3. Receive locationTimezone from backend and sync the system timezone
4. Allow jam-content-manager to detect when it should fetch new content

Key behaviors:
- Sends heartbeat every HEARTBEAT_INTERVAL_MINUTES minutes
- Uses Ed25519 signed requests to authenticate with the backend
- Writes screen_id.txt when screenId changes in response
- Writes location_timezone.txt and applies timedatectl when timezone changes
- Avoids excessive logging when offline (backs off after repeated failures)
- Notifies systemd watchdog to prove liveness
- Designed to be extremely stable - never crashes, always recovers

This service runs when /etc/jam/device_data/.announced exists (enforced by systemd).
This means heartbeats start as soon as the device announces itself, not waiting for registration.
"""

import sys
import os
import time
import signal
import subprocess

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sdnotify

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    is_device_announced,
    update_screen_id_if_changed,
    update_timezone_if_changed,
    get_location_timezone,
)
from common.api import api_request

logger = setup_service_logging('jam-heartbeat')

# How often to send heartbeats
HEARTBEAT_INTERVAL_MINUTES = 3
HEARTBEAT_INTERVAL_SECONDS = HEARTBEAT_INTERVAL_MINUTES * 60

# After this many consecutive failures, reduce logging verbosity
FAILURE_LOG_THRESHOLD = 3

# How long to wait before retrying after failure (starts at 30s, backs off)
INITIAL_RETRY_DELAY = 30
MAX_RETRY_DELAY = HEARTBEAT_INTERVAL_SECONDS  # Cap at normal interval

# Systemd notify
notifier = sdnotify.SystemdNotifier()

# Track if we should keep running
running = True


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global running
    logger.info(f"Received signal {signum}, shutting down...")
    running = False


def send_heartbeat() -> tuple[bool, str | None, str | None]:
    """
    Send a heartbeat to the backend.

    Returns:
        Tuple of (success, screen_id_from_response, location_timezone_from_response)
        screen_id may be None if device is not linked to a screen.
        location_timezone may be None if device is not assigned to a location.
    """
    response = api_request(
        method='POST',
        path='/jam-players/heartbeat',
        body=None,  # No request body
        signed=True
    )

    if response is None:
        return False, None, None

    if response.status_code == 200:
        try:
            data = response.json()
            screen_id = data.get('screenId')
            location_timezone = data.get('locationTimezone')
            return True, screen_id, location_timezone
        except Exception as e:
            logger.error(f"Error parsing heartbeat response: {e}")
            return True, None, None  # Request succeeded, just couldn't parse response
    else:
        logger.warning(f"Heartbeat returned status {response.status_code}")
        return False, None, None


def apply_system_timezone(timezone: str) -> bool:
    """
    Apply a timezone to the system using timedatectl.

    Args:
        timezone: IANA timezone identifier (e.g., "America/New_York")

    Returns:
        True if timezone was applied successfully, False otherwise.
    """
    try:
        result = subprocess.run(
            ['timedatectl', 'set-timezone', timezone],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            logger.info(f"System timezone set to: {timezone}")
            return True
        else:
            logger.error(f"Failed to set timezone: {result.stderr.strip()}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Timeout setting system timezone")
        return False
    except FileNotFoundError:
        logger.error("timedatectl not found - cannot set system timezone")
        return False
    except Exception as e:
        logger.error(f"Error setting system timezone: {e}")
        return False


def main():
    global running

    log_service_start(logger, 'JAM Heartbeat Service')

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Verify device is announced (shouldn't happen due to ConditionPathExists, but be safe)
    if not is_device_announced():
        logger.error("Device is not announced - heartbeat service should not be running")
        sys.exit(1)

    # Tell systemd we're ready IMMEDIATELY - don't block on timezone application
    notifier.notify("READY=1")
    logger.info(f"Service started, sending heartbeats every {HEARTBEAT_INTERVAL_MINUTES} minutes")

    # Apply stored timezone on startup (in case device rebooted)
    # This happens AFTER READY=1 to avoid blocking service startup
    stored_timezone = get_location_timezone()
    if stored_timezone:
        logger.info(f"Applying stored timezone on startup: {stored_timezone}")
        apply_system_timezone(stored_timezone)

    consecutive_failures = 0
    current_retry_delay = INITIAL_RETRY_DELAY

    while running:
        # Send heartbeat
        success, screen_id, location_timezone = send_heartbeat()

        if success:
            if consecutive_failures > 0:
                logger.info(f"Heartbeat succeeded after {consecutive_failures} failures")
            consecutive_failures = 0
            current_retry_delay = INITIAL_RETRY_DELAY

            # Update screen_id if changed
            update_screen_id_if_changed(screen_id)

            # Update timezone if changed
            if update_timezone_if_changed(location_timezone):
                # Timezone changed - apply it to the system
                if location_timezone:
                    apply_system_timezone(location_timezone)

            # Notify systemd watchdog
            notifier.notify("WATCHDOG=1")

            # Wait for next heartbeat interval
            wait_time = HEARTBEAT_INTERVAL_SECONDS
        else:
            consecutive_failures += 1

            # Only log every FAILURE_LOG_THRESHOLD failures to avoid log spam when offline
            if consecutive_failures <= FAILURE_LOG_THRESHOLD:
                logger.warning(f"Heartbeat failed (attempt {consecutive_failures})")
            elif consecutive_failures == FAILURE_LOG_THRESHOLD + 1:
                logger.warning(
                    f"Heartbeat has failed {consecutive_failures} times. "
                    "Reducing log verbosity until connection is restored."
                )
            # Still notify watchdog - we're alive, just can't reach the backend
            notifier.notify("WATCHDOG=1")

            # Back off retry delay (exponential with cap)
            wait_time = min(current_retry_delay, MAX_RETRY_DELAY)
            current_retry_delay = min(current_retry_delay * 2, MAX_RETRY_DELAY)

        # Sleep in small increments so we can respond to signals
        sleep_until = time.time() + wait_time
        while running and time.time() < sleep_until:
            remaining = sleep_until - time.time()
            time.sleep(min(15, remaining) if remaining > 0 else 0)

    logger.info("Heartbeat service shutting down")
    notifier.notify("STOPPING=1")


if __name__ == '__main__':
    main()
