#!/usr/bin/env python3
"""
JAM Health Monitor Service

Monitors all JAM Player systemd services and attempts recovery when they fail.
Reports critical failures to the backend API for alerting.

=== What This Service Does ===

1. Periodically checks the status of all JAM services
2. Attempts to restart failed services
3. Tracks failure counts to avoid crash loops
4. Logs failures locally and to the backend API

=== Monitored Services ===

- jam-ble-provisioning.service: WiFi setup via Bluetooth (monitor only, no restart)
- jam-ble-state-manager.service: Controls BLE based on network state
- jam-content-manager.service: Downloads and manages content
- jam-player-display.service: Video playback

Note: jam-ble-provisioning is monitored for crash/failure detection and backend
reporting, but NOT restarted by this service. Its running state is controlled
by jam-ble-state-manager based on network connectivity.

=== Failure Handling ===

If a service fails:
1. Log the failure
2. Attempt to restart it
3. Track failure count (per service)
4. If > 3 failures in 5 minutes, stop trying to restart (avoid crash loop)
5. Report critical failure to backend API

=== Service Lifecycle ===

This service runs continuously after boot check completes.
It must handle:
- Graceful shutdown on SIGTERM
- systemd watchdog pings
- Network-aware API reporting (skip if offline)
"""

import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

# Add services directory to path for common module imports
sys.path.insert(0, str(Path(__file__).parent))

from common.logging_config import setup_service_logging, log_service_start
from common.system import (
    get_service_status,
    get_systemd_notifier,
    setup_signal_handlers,
    WatchdogPinger,
)
from common.network import check_internet_connectivity
from common.api import api_request

# ============================================================================
# Logging Configuration
# ============================================================================

logger = setup_service_logging('jam-health-monitor')

# ============================================================================
# systemd Integration
# ============================================================================

sd_notifier = get_systemd_notifier()

# ============================================================================
# Constants
# ============================================================================

# Services to monitor for failures and report to backend
MONITORED_SERVICES = [
    'jam-ble-provisioning.service',
    'jam-ble-state-manager.service',
    'jam-content-manager.service',
    'jam-player-display.service',
    'jam-heartbeat.service',
    'jam-announce.service',
    'jam-ws-commands.service',
]

# Services that should always be running and will be restarted if failed
# Note: jam-ble-provisioning is NOT in this list because its running state
# is controlled by jam-ble-state-manager based on network connectivity.
# Note: jam-announce is NOT in this list because it's a oneshot service
# that only runs once (when .announced flag doesn't exist).
# Note: jam-heartbeat is NOT in this list because it only runs when
# the .registered flag exists.
# We still monitor all these for failures but don't try to restart them directly.
ALWAYS_RUNNING_SERVICES = [
    'jam-ble-state-manager.service',
    'jam-content-manager.service',
    'jam-player-display.service',
]

# Failure thresholds
MAX_FAILURES_IN_WINDOW = 3      # Stop restarting after this many failures
FAILURE_WINDOW_MINUTES = 5      # Time window for counting failures

# Check interval
CHECK_INTERVAL_SECONDS = 30     # How often to check service status

# Watchdog interval (seconds)
WATCHDOG_INTERVAL = 30


# ============================================================================
# Error Severity Enum (matches jam_player_error_severity in DB)
# ============================================================================

class ErrorSeverity(Enum):
    """
    Severity levels for JAM Player errors reported to the backend.

    These values match the jam_player_error_severity enum in the database:
    - CRITICAL: Critical service completely failed, not recoverable
    - HIGH: Service failed but was recovered, or restart failed
    - MEDIUM: Transient issue, may self-resolve
    - LOW: Informational, no action needed
    """
    CRITICAL = 'CRITICAL'
    HIGH = 'HIGH'
    MEDIUM = 'MEDIUM'
    LOW = 'LOW'


# ============================================================================
# Failure Tracking
# ============================================================================

@dataclass
class ServiceFailureTracker:
    """
    Tracks failures for a single service to detect crash loops.

    If a service fails more than MAX_FAILURES_IN_WINDOW times within
    FAILURE_WINDOW_MINUTES, we stop trying to restart it to avoid
    a crash loop that wastes resources and fills logs.
    """
    service_name: str
    failure_times: list = field(default_factory=list)
    gave_up: bool = False
    gave_up_at: Optional[datetime] = None

    def record_failure(self) -> bool:
        """
        Record a failure and return True if we should still try to restart.

        Returns:
            True if restart should be attempted, False if we've given up
        """
        now = datetime.now()

        # Clear old failures outside the window
        cutoff = now - timedelta(minutes=FAILURE_WINDOW_MINUTES)
        self.failure_times = [t for t in self.failure_times if t > cutoff]

        # Add this failure
        self.failure_times.append(now)

        # Check if we've exceeded the threshold
        if len(self.failure_times) > MAX_FAILURES_IN_WINDOW:
            self.gave_up = True
            self.gave_up_at = now
            return False

        return True

    def get_failure_count(self) -> int:
        """Get current failure count within the window."""
        now = datetime.now()
        cutoff = now - timedelta(minutes=FAILURE_WINDOW_MINUTES)
        return len([t for t in self.failure_times if t > cutoff])


# ============================================================================
# Health Monitor
# ============================================================================

class HealthMonitor:
    """
    Monitors JAM Player services and handles failures.
    """

    def __init__(self):
        """Initialize the health monitor."""
        self._running = True
        self._trackers: dict[str, ServiceFailureTracker] = {
            svc: ServiceFailureTracker(service_name=svc)
            for svc in MONITORED_SERVICES
        }

    def _get_service_status(self, service: str) -> tuple[bool, str]:
        """
        Get the status of a systemd service.

        Args:
            service: Service name (e.g., 'jam-content-manager.service')

        Returns:
            Tuple of (is_running, status_string)
        """
        status = get_service_status(service)
        if status is None:
            return False, 'error'
        is_running = (status == 'active')
        return is_running, status

    def _restart_service(self, service: str) -> bool:
        """
        Attempt to restart a systemd service.

        Args:
            service: Service name

        Returns:
            True if restart command succeeded
        """
        try:
            logger.info(f"Attempting to restart {service}")
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', service],
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode == 0:
                logger.info(f"Successfully restarted {service}")
                return True
            else:
                logger.error(f"Failed to restart {service}: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout restarting {service}")
            return False
        except Exception as e:
            logger.error(f"Error restarting {service}: {e}")
            return False

    def _report_to_backend(self, service: str, severity: ErrorSeverity, message: str):
        """
        Report an error to the backend API.

        This is best-effort - if we're offline or the API is down,
        we just log locally and continue.

        Args:
            service: The affected service name
            severity: Error severity level
            message: Human-readable error message
        """
        # First check if we have internet connectivity
        has_internet, _ = check_internet_connectivity()
        if not has_internet:
            logger.debug("No internet connectivity, skipping backend report")
            return

        try:
            response = api_request(
                method='POST',
                path='/jam-players/errors',
                body={
                    'affectedService': service,
                    'severity': severity.value,
                    'errorMessage': message,
                },
                timeout=10,
            )

            if response is None:
                logger.warning("Failed to report error to backend (request failed)")
            elif response.status_code == 200:
                logger.info(f"Reported error to backend: {service} ({severity.value})")
            else:
                logger.warning(
                    f"Backend returned {response.status_code} when reporting error: "
                    f"{response.text[:200] if response.text else 'no response body'}"
                )
        except Exception as e:
            logger.warning(f"Failed to report to backend: {e}")

    def _should_attempt_restart(self, service: str) -> bool:
        """
        Check if we should attempt to restart a failed service.

        Services in ALWAYS_RUNNING_SERVICES will be restarted when failed.
        Services like jam-ble-provisioning are monitored for failure reporting
        but NOT restarted, since their state is managed by another service.

        Args:
            service: Service name

        Returns:
            True if we should attempt to restart this service
        """
        return service in ALWAYS_RUNNING_SERVICES

    def check_services(self):
        """
        Check all monitored services and handle any failures.

        For services in ALWAYS_RUNNING_SERVICES: attempt restart on failure.
        For other services (like jam-ble-provisioning): log and report failures
        but don't attempt restart since they're managed by other services.
        """
        for service in MONITORED_SERVICES:
            tracker = self._trackers[service]

            # Skip if we've given up on this service
            if tracker.gave_up:
                continue

            # Get current status
            is_running, status = self._get_service_status(service)

            # Check for failed status (not just inactive)
            # "failed" means the service crashed, "inactive" could be intentional
            is_failed = (status == 'failed')

            if is_running:
                # Service is healthy
                continue

            # For services we don't manage, only report if actually failed (crashed)
            # "inactive" is expected for jam-ble-provisioning when online
            if not self._should_attempt_restart(service):
                if is_failed:
                    # Service crashed - log and report, but don't restart
                    logger.warning(f"Service {service} has failed (managed by another service)")
                    tracker.record_failure()
                    self._report_to_backend(
                        service,
                        ErrorSeverity.HIGH,
                        f"Service {service} failed (status={status}) - managed by another service"
                    )
                continue

            # Service should be running but isn't
            logger.warning(f"Service {service} is {status}, should be running")

            # Record the failure and check if we should restart
            should_restart = tracker.record_failure()
            failure_count = tracker.get_failure_count()

            if should_restart:
                # Attempt restart
                logger.info(
                    f"Attempting restart of {service} "
                    f"(failure {failure_count}/{MAX_FAILURES_IN_WINDOW})"
                )

                restart_success = self._restart_service(service)

                if restart_success:
                    # Report as HIGH (failed but recovered)
                    self._report_to_backend(
                        service,
                        ErrorSeverity.HIGH,
                        f"Service {service} failed (status={status}) and was restarted"
                    )
                else:
                    # Report restart failure
                    self._report_to_backend(
                        service,
                        ErrorSeverity.HIGH,
                        f"Service {service} failed (status={status}) and restart failed"
                    )
            else:
                # We've exceeded the failure threshold - give up
                logger.error(
                    f"Service {service} has failed {failure_count} times "
                    f"in {FAILURE_WINDOW_MINUTES} minutes - giving up on restarts"
                )

                # Report as SYSTEM_DOWN
                self._report_to_backend(
                    service,
                    ErrorSeverity.CRITICAL,
                    f"Service {service} failed {failure_count} times in "
                    f"{FAILURE_WINDOW_MINUTES} minutes - restart attempts stopped"
                )

    def get_status_summary(self) -> str:
        """Get a summary of monitored service statuses."""
        statuses = []
        for service in MONITORED_SERVICES:
            is_running, status = self._get_service_status(service)
            tracker = self._trackers[service]

            if tracker.gave_up:
                statuses.append(f"{service}: GAVE_UP")
            elif is_running:
                statuses.append(f"{service}: OK")
            else:
                statuses.append(f"{service}: {status}")

        return ", ".join(statuses)

    def stop(self):
        """Signal the monitor to stop."""
        self._running = False

    def run(self):
        """
        Main monitoring loop.
        """
        logger.info("Health monitor starting")

        # Notify systemd we're ready
        sd_notifier.notify("READY=1")

        watchdog = WatchdogPinger(WATCHDOG_INTERVAL)

        while self._running:
            try:
                # Check all services
                self.check_services()

                # Update status
                status_summary = self.get_status_summary()
                sd_notifier.notify(f"STATUS={status_summary}")

                # Ping watchdog if due
                watchdog.ping_if_due()

                # Sleep until next check
                time.sleep(CHECK_INTERVAL_SECONDS)

            except Exception as e:
                logger.exception(f"Error in health monitor loop: {e}")
                time.sleep(CHECK_INTERVAL_SECONDS)

        logger.info("Health monitor stopped")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point for the health monitor service."""
    log_service_start(logger, 'JAM Health Monitor Service')

    # Create monitor
    monitor = HealthMonitor()

    # Setup graceful shutdown
    setup_signal_handlers(monitor.stop, logger)

    # Run monitor
    try:
        monitor.run()
    except Exception as e:
        logger.exception(f"Fatal error in health monitor: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
