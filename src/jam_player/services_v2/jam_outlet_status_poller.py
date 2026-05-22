#!/usr/bin/env python3
"""
JAM Player Outlet Operational Status Poller

Long-running service that polls GET /jam-players/outlet-status every
OUTLET_POLL_INTERVAL_MINUTES minutes (default 6). Acts as a backstop
for missed WebSocket SET_OUTLET_OPERATIONAL_STATUS commands -- the WS
fast-path drives most updates, this picks up anything that fell on the
floor.

Why a dedicated poller (rather than piggybacking on heartbeat):
    - Heartbeat already does a lot (screen ID, timezone, orientation).
      Adding another "I'm alive" signal here keeps responsibilities
      separated -- if outlet status logic ever needs a different
      cadence or independent retry policy, it has its own home.
    - The 6-minute cadence is deliberately slower than heartbeat (2
      min). Outlet status changes are rare and the WS push handles the
      immediate case; the poller exists for the silent-WS failure
      mode where there's no urgency.

Updates the cache files in common/outlet_status.py and restarts
jam-player-display.service when the cached status changes so the
device transitions in or out of OUTLET_INACTIVE immediately.

This service requires:
    - .internet_verified flag (we can't poll without internet)
    - .registered flag (an unregistered device has no outlet)
"""

import os
import signal
import subprocess
import sys
import time

# Add the services directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sdnotify

from common.api import api_request
from common.logging_config import log_service_start, setup_service_logging
from common.outlet_status import (
    write_outlet_name_if_changed,
    write_outlet_status_if_changed,
)

logger = setup_service_logging("jam-outlet-status-poller")

# 6 minutes. Slower than heartbeat (2 min) because outlet status changes
# are rare and the WebSocket fast-path normally handles the immediate
# case. This polling is purely a backstop for missed WS messages.
OUTLET_POLL_INTERVAL_MINUTES = 6
OUTLET_POLL_INTERVAL_SECONDS = OUTLET_POLL_INTERVAL_MINUTES * 60

# After this many consecutive failures, reduce logging verbosity.
FAILURE_LOG_THRESHOLD = 3

# Retry-after-failure backoff (capped at the normal poll interval).
INITIAL_RETRY_DELAY = 30
MAX_RETRY_DELAY = OUTLET_POLL_INTERVAL_SECONDS

notifier = sdnotify.SystemdNotifier()

running = True


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    global running
    logger.info(f"Received signal {signum}, shutting down...")
    running = False


def poll_outlet_status() -> tuple[bool, dict]:
    """
    Hit GET /jam-players/outlet-status.

    Returns (success, parsed_response_dict). The dict shape (all
    optional from a fielded device's perspective):
        outletOperationalStatus: {"value": str, "label": str} | None
        outletName: str | None
    """
    response = api_request(
        method="GET",
        path="/jam-players/outlet-status",
        body=None,
        signed=True,
    )

    if response is None:
        return False, {}

    if response.status_code == 200:
        try:
            return True, response.json()
        except Exception as e:
            logger.error(f"Error parsing outlet-status response: {e}")
            return True, {}
    else:
        logger.warning(
            f"outlet-status returned status {response.status_code}"
        )
        return False, {}


def restart_display_service() -> None:
    """Signal jam-player-display to re-evaluate display mode."""
    try:
        subprocess.run(
            ["systemctl", "restart", "jam-player-display.service"],
            timeout=10,
            capture_output=True,
        )
    except Exception as e:
        logger.warning(f"Failed to restart display service: {e}")


def main():
    global running

    log_service_start(logger, "JAM Outlet Status Poller")

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    notifier.notify("READY=1")
    logger.info(
        f"Service started, polling outlet status every "
        f"{OUTLET_POLL_INTERVAL_MINUTES} minutes"
    )

    consecutive_failures = 0
    current_retry_delay = INITIAL_RETRY_DELAY

    while running:
        success, data = poll_outlet_status()

        if success:
            if consecutive_failures > 0:
                logger.info(
                    f"Outlet-status poll succeeded after "
                    f"{consecutive_failures} failures"
                )
            consecutive_failures = 0
            current_retry_delay = INITIAL_RETRY_DELAY

            outlet_status = data.get("outletOperationalStatus")
            outlet_name = data.get("outletName")

            if outlet_name is not None:
                write_outlet_name_if_changed(outlet_name)

            status_changed = False
            if isinstance(outlet_status, dict):
                value = outlet_status.get("value")
                label = outlet_status.get("label")
                if isinstance(value, str) and isinstance(label, str):
                    status_changed = write_outlet_status_if_changed(
                        value, label
                    )

            if status_changed:
                logger.info(
                    "Outlet status changed via poll -- restarting "
                    "jam-player-display.service"
                )
                restart_display_service()

            notifier.notify("WATCHDOG=1")
            wait_time = OUTLET_POLL_INTERVAL_SECONDS
        else:
            consecutive_failures += 1

            if consecutive_failures <= FAILURE_LOG_THRESHOLD:
                logger.warning(
                    f"Outlet-status poll failed "
                    f"(attempt {consecutive_failures})"
                )
            elif consecutive_failures == FAILURE_LOG_THRESHOLD + 1:
                logger.warning(
                    f"Outlet-status poll has failed "
                    f"{consecutive_failures} times. Reducing log "
                    f"verbosity until connection is restored."
                )

            notifier.notify("WATCHDOG=1")

            wait_time = min(current_retry_delay, MAX_RETRY_DELAY)
            current_retry_delay = min(
                current_retry_delay * 2, MAX_RETRY_DELAY
            )

        # Sleep in small increments so SIGTERM is responsive.
        sleep_until = time.time() + wait_time
        while running and time.time() < sleep_until:
            remaining = sleep_until - time.time()
            time.sleep(min(15, remaining) if remaining > 0 else 0)

    logger.info("Outlet status poller shutting down")
    notifier.notify("STOPPING=1")


if __name__ == "__main__":
    main()
