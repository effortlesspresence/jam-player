"""
JAM Player 2.0 - System Utilities

Functions for system checks, systemd service management, and common
service infrastructure (signal handlers, watchdog).
"""

import subprocess
import signal
import logging
from typing import Tuple, List, Optional, Callable

import sdnotify

logger = logging.getLogger(__name__)

# Shared systemd notifier instance
_sd_notifier: Optional[sdnotify.SystemdNotifier] = None


def get_systemd_notifier() -> sdnotify.SystemdNotifier:
    """
    Get the shared systemd notifier instance.

    Returns:
        SystemdNotifier instance for communicating with systemd.
    """
    global _sd_notifier
    if _sd_notifier is None:
        _sd_notifier = sdnotify.SystemdNotifier()
    return _sd_notifier


def setup_signal_handlers(on_shutdown: Callable[[], None], service_logger: Optional[logging.Logger] = None) -> None:
    """
    Setup graceful shutdown signal handlers for SIGTERM and SIGINT.

    This is a common pattern needed by all long-running services to handle
    systemd stop requests and Ctrl+C gracefully.

    Args:
        on_shutdown: Callback function to invoke when shutdown is requested.
                     This should trigger graceful service shutdown (e.g., quit
                     the main loop, set a running flag to False).
        service_logger: Optional logger to use for shutdown message.
                        Defaults to module logger if not provided.

    Example:
        def main():
            running = True

            def shutdown():
                nonlocal running
                running = False

            setup_signal_handlers(shutdown)

            while running:
                do_work()
    """
    log = service_logger or logger

    def shutdown_handler(signum, frame):
        sig_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
        log.info(f"Received {sig_name}, initiating graceful shutdown")
        on_shutdown()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def setup_glib_watchdog(interval_seconds: int = 30) -> None:
    """
    Setup systemd watchdog pinging using GLib timeout.

    For services using GLib main loop (D-Bus services like BLE provisioning
    and BLE state manager), this sets up periodic watchdog pings.

    Args:
        interval_seconds: How often to ping the watchdog. Should be less than
                          the WatchdogSec value in the systemd unit file.

    Note:
        Must be called after GLib is imported and before the main loop starts.
        Requires: from gi.repository import GLib
    """
    # Import GLib here to avoid requiring it for services that don't use it
    from gi.repository import GLib

    notifier = get_systemd_notifier()

    def ping_watchdog() -> bool:
        notifier.notify("WATCHDOG=1")
        return True  # Return True to keep the timeout active

    GLib.timeout_add_seconds(interval_seconds, ping_watchdog)
    logger.debug(f"Configured GLib watchdog ping every {interval_seconds}s")


class WatchdogPinger:
    """
    Watchdog pinger for services using time.sleep()-based loops.

    For services that don't use GLib (like health monitor), this provides
    a simple way to track when to ping the watchdog.

    Example:
        watchdog = WatchdogPinger(interval_seconds=30)

        while running:
            do_work()
            watchdog.ping_if_due()
            time.sleep(check_interval)
    """

    def __init__(self, interval_seconds: int = 30):
        """
        Initialize the watchdog pinger.

        Args:
            interval_seconds: How often to ping the watchdog.
        """
        import time
        self.interval = interval_seconds
        self._last_ping = time.time()
        self._notifier = get_systemd_notifier()

    def ping_if_due(self) -> bool:
        """
        Ping the watchdog if enough time has passed since last ping.

        Returns:
            True if a ping was sent, False otherwise.
        """
        import time
        current_time = time.time()
        if current_time - self._last_ping >= self.interval:
            self._notifier.notify("WATCHDOG=1")
            self._last_ping = current_time
            return True
        return False

    def ping(self) -> None:
        """Force an immediate watchdog ping."""
        import time
        self._notifier.notify("WATCHDOG=1")
        self._last_ping = time.time()

# Default timeouts
DEFAULT_COMMAND_TIMEOUT = 10  # seconds
DEFAULT_SERVICE_ACTION_TIMEOUT = 30  # seconds

# Required system services for JAM Player operation
REQUIRED_SYSTEM_SERVICES = [
    'bluetooth.service',
    'NetworkManager.service',
    'chrony.service',
]


def check_chrony_sync() -> bool:
    """
    Check if the system clock is synchronized via chrony.

    This is important for video wall synchronization where all devices
    must maintain <50ms clock accuracy.

    Returns:
        True if chrony reports "Leap status: Normal" (synced).
    """
    try:
        result = subprocess.run(
            ['chronyc', 'tracking'],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )

        if result.returncode != 0:
            logger.warning(f"chronyc tracking failed: {result.stderr}")
            return False

        output = result.stdout

        # Check for "Leap status     : Normal" which indicates sync
        if 'Leap status' in output:
            for line in output.split('\n'):
                if 'Leap status' in line:
                    status = line.split(':')[1].strip().lower()
                    if status == 'normal':
                        # Don't log success - it's the expected state
                        return True
                    else:
                        logger.warning(f"Chrony leap status: {status}")
                        return False

        logger.warning("Could not determine chrony sync status")
        return False

    except subprocess.TimeoutExpired:
        logger.warning("chronyc command timed out")
        return False
    except FileNotFoundError:
        logger.warning("chronyc not found - chrony may not be installed")
        return False
    except Exception as e:
        logger.warning(f"Error checking chrony sync: {e}")
        return False


def check_service_active(service_name: str) -> bool:
    """
    Check if a systemd service is currently active.

    Args:
        service_name: Name of the service (e.g., 'bluetooth.service')

    Returns:
        True if service is active.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )
        return result.stdout.strip() == 'active'

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout checking {service_name}")
        return False
    except Exception as e:
        logger.warning(f"Error checking {service_name}: {e}")
        return False


def check_service_exists(service_name: str) -> bool:
    """
    Check if a systemd service unit file exists.

    Args:
        service_name: Name of the service (e.g., 'jam-ble-provisioning.service')

    Returns:
        True if service exists in systemd.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'list-unit-files', service_name],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )
        return service_name in result.stdout

    except Exception as e:
        logger.warning(f"Error checking if {service_name} exists: {e}")
        return False


def check_required_services() -> Tuple[bool, List[str]]:
    """
    Check if all required system services are running.

    Returns:
        Tuple of (all_running, list_of_failed_services)
    """
    failed_services = []

    for service in REQUIRED_SYSTEM_SERVICES:
        if not check_service_active(service):
            logger.warning(f"Required service {service} is not active")
            failed_services.append(service)
        # Don't log when services are active - that's the expected state

    return len(failed_services) == 0, failed_services


def start_service(service_name: str) -> bool:
    """
    Start a systemd service.

    Args:
        service_name: Name of the service to start

    Returns:
        True if service started successfully.
    """
    try:
        logger.info(f"Starting {service_name}...")
        result = subprocess.run(
            ['systemctl', 'start', service_name],
            capture_output=True,
            text=True,
            timeout=DEFAULT_SERVICE_ACTION_TIMEOUT
        )

        if result.returncode != 0:
            logger.error(f"Failed to start {service_name}: {result.stderr}")
            return False

        logger.info(f"Successfully started {service_name}")
        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout starting {service_name}")
        return False
    except Exception as e:
        logger.error(f"Error starting {service_name}: {e}")
        return False


def stop_service(service_name: str) -> bool:
    """
    Stop a systemd service.

    Args:
        service_name: Name of the service to stop

    Returns:
        True if service stopped successfully.
    """
    try:
        logger.info(f"Stopping {service_name}...")
        result = subprocess.run(
            ['systemctl', 'stop', service_name],
            capture_output=True,
            text=True,
            timeout=DEFAULT_SERVICE_ACTION_TIMEOUT
        )

        if result.returncode != 0:
            logger.error(f"Failed to stop {service_name}: {result.stderr}")
            return False

        logger.info(f"Successfully stopped {service_name}")
        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout stopping {service_name}")
        return False
    except Exception as e:
        logger.error(f"Error stopping {service_name}: {e}")
        return False


def restart_service(service_name: str) -> bool:
    """
    Restart a systemd service.

    Args:
        service_name: Name of the service to restart

    Returns:
        True if service restarted successfully.
    """
    try:
        logger.info(f"Restarting {service_name}...")
        result = subprocess.run(
            ['systemctl', 'restart', service_name],
            capture_output=True,
            text=True,
            timeout=DEFAULT_SERVICE_ACTION_TIMEOUT
        )

        if result.returncode != 0:
            logger.error(f"Failed to restart {service_name}: {result.stderr}")
            return False

        logger.info(f"Successfully restarted {service_name}")
        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout restarting {service_name}")
        return False
    except Exception as e:
        logger.error(f"Error restarting {service_name}: {e}")
        return False


def manage_service(service_name: str, should_run: bool) -> bool:
    """
    Ensure a service is in the desired state (running or stopped).

    Args:
        service_name: Name of the service
        should_run: True to ensure running, False to ensure stopped

    Returns:
        True if service is in desired state.
    """
    # Check if service exists
    if not check_service_exists(service_name):
        logger.warning(f"{service_name} is not installed")
        return False

    is_active = check_service_active(service_name)

    if should_run and is_active:
        # Don't log - this is the normal/expected state during periodic checks
        return True
    elif not should_run and not is_active:
        # Don't log - this is the normal/expected state during periodic checks
        return True
    elif should_run:
        return start_service(service_name)
    else:
        return stop_service(service_name)


def get_service_status(service_name: str) -> Optional[str]:
    """
    Get the current status of a systemd service.

    Args:
        service_name: Name of the service

    Returns:
        Status string (active, inactive, failed, etc.) or None on error.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )
        return result.stdout.strip()

    except Exception as e:
        logger.warning(f"Error getting status of {service_name}: {e}")
        return None
