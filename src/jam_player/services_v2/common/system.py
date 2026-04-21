"""
JAM Player 2.0 - System Utilities

Functions for system checks, systemd service management, and common
service infrastructure (signal handlers, watchdog).
"""

import subprocess
import signal
import logging
from pathlib import Path
from typing import Tuple, List, Optional, Callable

from .paths import safe_write_text

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


def clear_network_impairments() -> bool:
    """
    Clear any network traffic control (tc) impairments and stop chaos processes.

    This is called on every boot to ensure that network simulation settings
    from jam-simulate-network are not accidentally left enabled when creating
    device images.

    Returns:
        True if cleared successfully (or nothing to clear), False on error.
    """
    # First, stop any running chaos process from jam-simulate-network
    chaos_pid_file = '/tmp/jam-network-chaos.pid'
    try:
        if Path(chaos_pid_file).exists():
            pid_str = Path(chaos_pid_file).read_text().strip()
            if pid_str:
                pid = int(pid_str)
                try:
                    import os as os_module
                    os_module.kill(pid, 9)  # SIGKILL
                    logger.info(f"Stopped network chaos process (PID {pid})")
                except ProcessLookupError:
                    pass  # Process already dead
                except Exception as e:
                    logger.debug(f"Could not kill chaos process {pid}: {e}")
            Path(chaos_pid_file).unlink(missing_ok=True)
    except Exception as e:
        logger.debug(f"Error checking chaos PID file: {e}")

    # Try wlan0 first, then eth0
    interfaces = ['wlan0', 'eth0']

    for iface in interfaces:
        try:
            # Check if interface exists
            check_result = subprocess.run(
                ['ip', 'link', 'show', iface],
                capture_output=True,
                timeout=DEFAULT_COMMAND_TIMEOUT
            )
            if check_result.returncode != 0:
                continue  # Interface doesn't exist

            # Ensure interface is up (chaos mode may have left it down)
            subprocess.run(
                ['ip', 'link', 'set', iface, 'up'],
                capture_output=True,
                timeout=DEFAULT_COMMAND_TIMEOUT
            )

            # Try to delete any tc qdisc rules
            result = subprocess.run(
                ['tc', 'qdisc', 'del', 'dev', iface, 'root'],
                capture_output=True,
                text=True,
                timeout=DEFAULT_COMMAND_TIMEOUT
            )

            # returncode 2 means "no qdisc to delete" which is fine
            if result.returncode == 0:
                logger.info(f"Cleared network impairments from {iface}")
            # Don't log if there was nothing to clear

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout clearing network impairments from {iface}")
        except FileNotFoundError:
            # tc command not found - shouldn't happen on a real JAM Player
            logger.debug("tc command not found")
            return True
        except Exception as e:
            logger.warning(f"Error clearing network impairments from {iface}: {e}")

    return True  # Always return True - failure to clear shouldn't block boot


def get_unique_hostname(device_uuid: str) -> str:
    """
    Generate a unique hostname from the device UUID.

    Uses the last 5 characters of the UUID (without hyphens) to create
    a hostname like 'jam-player-a1b2c'. This matches the BLE device naming
    convention (JAM-PLAYER-XXXXX).

    Args:
        device_uuid: The device's UUID string

    Returns:
        Hostname string like 'jam-player-a1b2c'
    """
    # Remove hyphens and get last 5 chars, lowercase for hostname
    clean_uuid = device_uuid.replace('-', '')
    suffix = clean_uuid[-5:].lower()
    return f"jam-player-{suffix}"


def set_unique_hostname(device_uuid: str) -> bool:
    """
    Set a unique hostname for this device based on its UUID.

    This fixes the iOS BLE pairing cache issue where all JAM Players had
    the same hostname (comitup-307), causing iOS to confuse devices and
    show "Peer removed pairing information" errors.

    The hostname is set to 'jam-player-XXXXX' where XXXXX is derived from
    the device UUID (same suffix used in BLE advertising name).

    Args:
        device_uuid: The device's UUID string

    Returns:
        True if hostname was set successfully or already correct
    """
    new_hostname = get_unique_hostname(device_uuid)

    try:
        # Check current hostname
        result = subprocess.run(
            ['hostname'],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )
        current_hostname = result.stdout.strip()

        if current_hostname == new_hostname:
            logger.info(f"Hostname already set to: {new_hostname}")
            return True

        logger.info(f"Setting hostname from '{current_hostname}' to '{new_hostname}'")

        # Use hostnamectl to set hostname (handles /etc/hostname and systemd)
        result = subprocess.run(
            ['hostnamectl', 'set-hostname', new_hostname],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )

        if result.returncode != 0:
            logger.error(f"hostnamectl failed: {result.stderr}")
            return False

        # Update /etc/hosts to replace old hostname with new one
        hosts_file = Path('/etc/hosts')
        if hosts_file.exists():
            hosts_content = hosts_file.read_text()
            lines = hosts_content.split('\n')
            new_lines = []
            found_127_0_1_1 = False

            for line in lines:
                if '127.0.1.1' in line:
                    # Replace the 127.0.1.1 line with new hostname
                    new_lines.append(f"127.0.1.1\t{new_hostname}")
                    found_127_0_1_1 = True
                elif '127.0.0.1' in line:
                    # Remove old hostname from 127.0.0.1 line (comitup puts it here)
                    # Keep localhost and any other entries, but remove comitup-XXX or old jam-player-XXX
                    parts = line.split()
                    # Keep 127.0.0.1 and filter out old hostnames
                    filtered_parts = [parts[0]]  # Keep the IP
                    for part in parts[1:]:
                        if not part.startswith('comitup-') and not part.startswith('jam-player-'):
                            filtered_parts.append(part)
                    new_lines.append('\t'.join(filtered_parts) if len(filtered_parts) > 1 else parts[0] + '\tlocalhost')
                else:
                    new_lines.append(line)

            # If there was no 127.0.1.1 line, add one for the hostname
            if not found_127_0_1_1:
                # Insert after the 127.0.0.1 line
                for i, line in enumerate(new_lines):
                    if '127.0.0.1' in line:
                        new_lines.insert(i + 1, f"127.0.1.1\t{new_hostname}")
                        break
                else:
                    # No 127.0.0.1 line found, just append
                    new_lines.append(f"127.0.1.1\t{new_hostname}")

            safe_write_text(hosts_file, '\n'.join(new_lines), 0o644)
            logger.info("Updated /etc/hosts with new hostname")

        # Also try to set the BlueZ adapter Alias so iOS/Android system
        # Bluetooth UI shows the per-device name (not the stale
        # "comitup-307" bluetoothd inferred from hostname at its own
        # startup). This is best-effort: bluetoothd may not be running
        # yet during jam-first-boot, in which case jam-ble-provisioning
        # will set the Alias via D-Bus when it later starts up.
        try:
            alias_result = subprocess.run(
                ['bluetoothctl', 'system-alias', new_hostname],
                capture_output=True,
                text=True,
                timeout=DEFAULT_COMMAND_TIMEOUT,
            )
            if alias_result.returncode == 0:
                logger.info(f"Bluetooth adapter alias set to: {new_hostname}")
            else:
                # Log at debug -- expected early in boot before bluetoothd is up
                logger.debug(
                    f"bluetoothctl system-alias non-zero "
                    f"(will be set later by jam-ble-provisioning): "
                    f"{alias_result.stderr.strip() or alias_result.stdout.strip()}"
                )
        except FileNotFoundError:
            logger.debug("bluetoothctl not installed -- skipping alias set")
        except subprocess.TimeoutExpired:
            logger.debug("bluetoothctl timed out -- will be set later by jam-ble-provisioning")
        except Exception as e:
            logger.debug(f"Could not set bluetooth alias via bluetoothctl: {e}")

        logger.info(f"Hostname set to: {new_hostname}")
        return True

    except subprocess.TimeoutExpired:
        logger.error("Timeout setting hostname")
        return False
    except Exception as e:
        logger.error(f"Failed to set hostname: {e}")
        return False
