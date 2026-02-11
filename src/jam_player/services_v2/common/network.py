"""
JAM Player 2.0 - Network Utilities

Shared network connectivity functions used across JP 2.0 services.
Uses NetworkManager via nmcli for network state detection.

Internet Connectivity Verification:
For critical decisions like enabling/disabling BLE provisioning, we don't
rely solely on NetworkManager state. Instead, we perform actual connectivity
tests against:
1. JAM backend health endpoint (primary - this is what actually matters)
2. Cloudflare DNS (1.1.1.1) and Google DNS (8.8.8.8) as fallbacks

This handles cases where:
- NetworkManager reports "connected" but there's no actual internet
- The JAM backend is down but internet is working (use fallbacks)
- Flaky restaurant WiFi that drops intermittently
"""

import subprocess
import socket
import time
import logging
from typing import Optional, Tuple, List, Dict

logger = logging.getLogger(__name__)

# Default timeouts
DEFAULT_NETWORK_WAIT_TIMEOUT = 30  # seconds
DEFAULT_COMMAND_TIMEOUT = 10  # seconds
DEFAULT_INTERNET_CHECK_TIMEOUT = 5  # seconds per check

# Fallback endpoints for internet connectivity verification
# Used when JAM backend is unreachable but we need to verify internet works
FALLBACK_DNS_SERVERS = [
    ("1.1.1.1", 53),   # Cloudflare DNS
    ("8.8.8.8", 53),   # Google DNS
]


def check_nm_connection_state() -> Tuple[bool, str]:
    """
    Check if NetworkManager reports an active network connection.

    This queries NetworkManager state via nmcli. It does NOT verify actual
    internet connectivity - use check_internet_connectivity() for that.

    Returns:
        Tuple of (has_connection, connection_type)
        connection_type is one of: 'wifi', 'ethernet', 'none', 'unknown', 'timeout', 'error'
    """
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'TYPE,STATE', 'device'],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )

        if result.returncode != 0:
            logger.warning(f"nmcli failed: {result.stderr}")
            return False, "unknown"

        lines = result.stdout.strip().split('\n')
        for line in lines:
            if ':connected' in line.lower():
                conn_type = line.split(':')[0].lower()
                if conn_type in ('wifi', 'ethernet'):
                    return True, conn_type

        return False, "none"

    except subprocess.TimeoutExpired:
        logger.warning("nmcli timed out")
        return False, "timeout"
    except FileNotFoundError:
        logger.error("nmcli not found - NetworkManager may not be installed")
        return False, "error"
    except Exception as e:
        logger.error(f"Error checking network connectivity: {e}")
        return False, "error"


def wait_for_network(timeout_seconds: int = DEFAULT_NETWORK_WAIT_TIMEOUT) -> Tuple[bool, str]:
    """
    Wait for network connectivity with timeout.

    Polls network state every 2 seconds until connected or timeout.

    Args:
        timeout_seconds: Maximum time to wait for connectivity

    Returns:
        Tuple of (is_connected, connection_type)
    """
    logger.debug(f"Waiting up to {timeout_seconds}s for network connectivity...")

    start_time = time.time()
    check_interval = 2  # seconds

    while time.time() - start_time < timeout_seconds:
        connected, conn_type = check_nm_connection_state()
        if connected:
            elapsed = time.time() - start_time
            logger.debug(f"Network connected via {conn_type} after {elapsed:.1f}s")
            return True, conn_type

        time.sleep(check_interval)

    logger.debug(f"No network connectivity after {timeout_seconds}s")
    return False, "none"


# Global cache for WiFi networks - used to avoid blocking BLE thread
_wifi_networks_cache: List[Dict[str, str]] = []
_wifi_scan_in_progress = False
_wifi_cache_lock = __import__('threading').Lock()


def get_available_wifi_networks() -> List[Dict[str, str]]:
    """
    Get available WiFi networks without blocking.

    Returns cached results immediately and triggers a background scan
    to update the cache. This is critical for BLE operations which
    cannot block for long periods without causing disconnects.

    Returns:
        List of dicts with keys: ssid, signal_strength, is_secured, security_type
        Returns cached list (may be empty on first call).
    """
    global _wifi_networks_cache, _wifi_scan_in_progress

    # Return cached results immediately (non-blocking)
    with _wifi_cache_lock:
        cached = list(_wifi_networks_cache)
        should_scan = not _wifi_scan_in_progress

    # Trigger background scan if not already running
    if should_scan:
        import threading
        thread = threading.Thread(target=_scan_wifi_networks_background, daemon=True)
        thread.start()

    logger.debug(f"Returning {len(cached)} cached WiFi networks")
    return cached


def _parse_nmcli_wifi_output(output: str) -> List[Dict[str, str]]:
    """
    Parse nmcli WiFi list output into a list of network dicts.

    Handles deduplication, signal strength conversion, and security detection.

    Args:
        output: Raw output from `nmcli -t -f SSID,SIGNAL,SECURITY device wifi list`

    Returns:
        List of network dicts sorted by signal strength (strongest first).
        Each dict has: ssid, signal_strength, is_secured, security_type
    """
    networks = []
    seen_ssids = set()  # Deduplicate networks

    for line in output.strip().split('\n'):
        if not line:
            continue

        parts = line.split(':')
        if len(parts) >= 3:
            ssid = parts[0]
            if ssid and ssid not in seen_ssids:
                seen_ssids.add(ssid)
                # nmcli returns signal as percentage (0-100), convert to dBm-like scale
                # 100% ≈ -30dBm, 0% ≈ -90dBm
                signal_percent = int(parts[1]) if parts[1].isdigit() else 50
                signal_dbm = -90 + int(signal_percent * 0.6)  # Map 0-100 to -90 to -30

                security = parts[2] if parts[2] else ''
                is_secured = bool(security and security.lower() != 'open' and security != '--')

                networks.append({
                    'ssid': ssid,
                    'signal_strength': signal_dbm,
                    'is_secured': is_secured,
                    'security_type': security if security else None
                })

    # Sort by signal strength (highest/closest to 0 first)
    networks.sort(key=lambda x: x['signal_strength'], reverse=True)
    return networks


def _scan_wifi_networks_background():
    """
    Scan for WiFi networks in background thread.
    Updates the global cache when complete.
    """
    global _wifi_networks_cache, _wifi_scan_in_progress

    with _wifi_cache_lock:
        if _wifi_scan_in_progress:
            return  # Already scanning
        _wifi_scan_in_progress = True

    try:
        logger.debug("Starting background WiFi scan...")

        # Trigger a fresh scan (this is fast, just initiates the scan)
        subprocess.run(
            ['nmcli', 'device', 'wifi', 'rescan'],
            capture_output=True,
            timeout=5
        )

        # Wait for scan to complete - this is in background thread so OK to block
        time.sleep(2)

        # Get list of networks
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            logger.warning(f"WiFi scan failed: {result.stderr}")
            return

        networks = _parse_nmcli_wifi_output(result.stdout)

        # Update cache
        with _wifi_cache_lock:
            _wifi_networks_cache = networks

        logger.debug(f"Background scan found {len(networks)} WiFi networks")

    except subprocess.TimeoutExpired:
        logger.warning("WiFi scan timed out")
    except Exception as e:
        logger.error(f"Error scanning WiFi networks: {e}")
    finally:
        with _wifi_cache_lock:
            _wifi_scan_in_progress = False


def trigger_wifi_scan():
    """
    Trigger a WiFi scan immediately (blocking).
    Use this at service startup to populate the cache before BLE connections.
    """
    global _wifi_networks_cache

    logger.info("Triggering initial WiFi scan...")

    try:
        # Trigger scan
        subprocess.run(
            ['nmcli', 'device', 'wifi', 'rescan'],
            capture_output=True,
            timeout=5
        )
        time.sleep(2)

        # Get results
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            networks = _parse_nmcli_wifi_output(result.stdout)

            # Update cache
            with _wifi_cache_lock:
                _wifi_networks_cache = networks

            logger.info(f"Initial scan found {len(networks)} WiFi networks")

    except Exception as e:
        logger.error(f"Initial WiFi scan failed: {e}")


def _log_network_diagnostic_info():
    """
    Log detailed network diagnostic information for debugging WiFi connection issues.
    This helps diagnose issues like "Connection activation failed: New connection activation was enqueued"
    """
    logger.info("=== NETWORK DIAGNOSTIC INFO ===")

    # 1. Log wlan0 interface state
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info(f"Device status:\n{result.stdout.strip()}")
        else:
            logger.warning(f"Failed to get device status: {result.stderr}")
    except Exception as e:
        logger.warning(f"Error getting device status: {e}")

    # 2. Log active connections
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info(f"Active connections:\n{result.stdout.strip() or '(none)'}")
        else:
            logger.warning(f"Failed to get active connections: {result.stderr}")
    except Exception as e:
        logger.warning(f"Error getting active connections: {e}")

    # 3. Log NetworkManager state
    try:
        result = subprocess.run(
            ['nmcli', 'general', 'status'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info(f"NetworkManager status:\n{result.stdout.strip()}")
        else:
            logger.warning(f"Failed to get NM status: {result.stderr}")
    except Exception as e:
        logger.warning(f"Error getting NM status: {e}")

    # 4. Check if comitup service is running
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'comitup'],
            capture_output=True,
            text=True,
            timeout=5
        )
        comitup_state = result.stdout.strip()
        logger.info(f"comitup.service state: {comitup_state}")
    except Exception as e:
        logger.warning(f"Error checking comitup state: {e}")

    # 5. Check rfkill status (is WiFi blocked?)
    try:
        result = subprocess.run(
            ['rfkill', 'list', 'wifi'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.info(f"rfkill wifi status:\n{result.stdout.strip()}")
        else:
            logger.warning(f"Failed to get rfkill status: {result.stderr}")
    except Exception as e:
        logger.warning(f"Error getting rfkill status: {e}")

    logger.info("=== END DIAGNOSTIC INFO ===")


def _stop_comitup_hotspot() -> bool:
    """
    Stop the comitup hotspot to free up the wlan0 interface for client mode.

    Returns:
        True if hotspot was stopped or wasn't running, False on error
    """
    try:
        # Check if comitup hotspot is active (connection name starts with JAM-SETUP)
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show', '--active'],
            capture_output=True,
            text=True,
            timeout=5
        )

        hotspot_name = None
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and parts[0].startswith('JAM-SETUP'):
                    hotspot_name = parts[0]
                    break

        if not hotspot_name:
            logger.info("No comitup hotspot active, proceeding with WiFi connection")
            return True

        logger.info(f"Stopping comitup hotspot: {hotspot_name}")

        # Bring down the hotspot connection
        result = subprocess.run(
            ['nmcli', 'connection', 'down', hotspot_name],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            logger.info(f"Successfully stopped hotspot: {hotspot_name}")
            # Give NetworkManager a moment to release the interface
            time.sleep(1)
            return True
        else:
            logger.warning(f"Failed to stop hotspot: {result.stderr}")
            return False

    except Exception as e:
        logger.error(f"Error stopping comitup hotspot: {e}")
        return False


def connect_to_wifi(ssid: str, password: str) -> Tuple[bool, str]:
    """
    Connect to a WiFi network, preserving existing connection if new attempt fails.

    If already connected to a working network and the new connection attempt fails,
    we restore the previous connection to avoid leaving the device offline.

    Args:
        ssid: Network name
        password: Network password

    Returns:
        Tuple of (success, error_message)
    """
    try:
        logger.info(f"Attempting to connect to WiFi network: {ssid}")

        # Save current connection state before attempting new connection
        previous_connection = _get_active_wifi_connection()
        if previous_connection:
            logger.info(f"Currently connected to: {previous_connection['name']} (will restore if new connection fails)")

        # Check if already connected to this exact network
        if previous_connection and previous_connection.get('ssid') == ssid:
            # Already connected to this network - verify it's working
            connected, _ = check_nm_connection_state()
            if connected:
                logger.info(f"Already connected to {ssid} with working connection - skipping reconnect")
                return True, ""
            logger.info(f"Connected to {ssid} but no internet - will attempt reconnect")

        # Log diagnostic info before attempting connection (helps debug failures)
        _log_network_diagnostic_info()

        # Stop comitup hotspot if running - can't use wlan0 for both AP and client mode
        hotspot_stopped = _stop_comitup_hotspot()
        logger.info(f"Hotspot stop result: {hotspot_stopped}")

        # Try to connect using nmcli
        logger.info(f"Running: nmcli device wifi connect '{ssid}' password '****'")
        result = subprocess.run(
            ['nmcli', 'device', 'wifi', 'connect', ssid, 'password', password],
            capture_output=True,
            text=True,
            timeout=30  # WiFi connection can take a while
        )

        logger.info(f"nmcli return code: {result.returncode}")
        logger.info(f"nmcli stdout: {result.stdout.strip()}")
        logger.info(f"nmcli stderr: {result.stderr.strip()}")

        if result.returncode == 0:
            logger.info(f"Successfully connected to {ssid}")
            return True, ""

        error_msg = result.stderr.strip() or result.stdout.strip()
        logger.error(f"WiFi connection FAILED for {ssid}: {error_msg}")

        # Log diagnostic info again after failure to see what changed
        logger.info("Post-failure diagnostic info:")
        _log_network_diagnostic_info()

        # If we had a previous working connection, try to restore it
        if previous_connection:
            logger.info(f"Restoring previous connection to: {previous_connection['name']}")
            _restore_wifi_connection(previous_connection['name'])

        # Parse common error messages for user-friendly feedback
        if 'Secrets were required' in error_msg or 'password' in error_msg.lower():
            return False, "Invalid password"
        elif 'No network with SSID' in error_msg:
            return False, "Network not found"
        else:
            return False, error_msg or "Connection failed"

    except subprocess.TimeoutExpired:
        logger.error(f"Connection to {ssid} timed out after 30 seconds")
        logger.info("Post-timeout diagnostic info:")
        _log_network_diagnostic_info()
        # Try to restore previous connection on timeout too
        if previous_connection:
            _restore_wifi_connection(previous_connection['name'])
        return False, "Connection timed out"
    except Exception as e:
        logger.error(f"Exception during WiFi connection: {type(e).__name__}: {e}")
        logger.info("Post-exception diagnostic info:")
        _log_network_diagnostic_info()
        return False, str(e)


def _get_active_wifi_connection() -> Optional[Dict[str, str]]:
    """Get the currently active WiFi connection profile."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 3 and parts[1] == '802-11-wireless':
                    # Get the SSID for this connection
                    ssid_result = subprocess.run(
                        ['nmcli', '-t', '-f', '802-11-wireless.ssid', 'connection', 'show', parts[0]],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    ssid = ''
                    if ssid_result.returncode == 0:
                        ssid_line = ssid_result.stdout.strip()
                        if ':' in ssid_line:
                            ssid = ssid_line.split(':', 1)[1]
                    return {'name': parts[0], 'ssid': ssid, 'device': parts[2]}
        return None
    except Exception as e:
        logger.warning(f"Could not get active WiFi connection: {e}")
        return None


def _restore_wifi_connection(connection_name: str) -> bool:
    """Restore a previously active WiFi connection."""
    try:
        logger.info(f"Attempting to restore connection: {connection_name}")
        result = subprocess.run(
            ['nmcli', 'connection', 'up', connection_name],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            logger.info(f"Successfully restored connection to {connection_name}")
            return True
        else:
            logger.warning(f"Failed to restore connection: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"Error restoring connection: {e}")
        return False


def get_current_connection_info() -> Optional[Dict[str, str]]:
    """
    Get information about the current network connection.

    Returns:
        Dict with connection info or None if not connected.
        Keys: type, name, ip_address
    """
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'TYPE,NAME,IP4.ADDRESS', 'connection', 'show', '--active'],
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMMAND_TIMEOUT
        )

        if result.returncode != 0 or not result.stdout.strip():
            return None

        # Parse the first active connection
        line = result.stdout.strip().split('\n')[0]
        parts = line.split(':')

        if len(parts) >= 2:
            conn_type = parts[0].lower()
            if conn_type in ('802-11-wireless', 'wifi'):
                conn_type = 'wifi'
            elif conn_type in ('802-3-ethernet', 'ethernet'):
                conn_type = 'ethernet'

            return {
                'type': conn_type,
                'name': parts[1],
                'ip_address': parts[2] if len(parts) > 2 else 'unknown'
            }

        return None

    except Exception as e:
        logger.error(f"Error getting connection info: {e}")
        return None


# ============================================================================
# Internet Connectivity Verification
# ============================================================================

def _check_tcp_connectivity(host: str, port: int, timeout: float) -> bool:
    """
    Check if we can establish a TCP connection to a host:port.

    This is a low-level check used as a fallback when HTTP checks fail.
    Connecting to DNS servers on port 53 is a reliable internet indicator.

    Args:
        host: IP address or hostname
        port: Port number
        timeout: Connection timeout in seconds

    Returns:
        True if TCP connection succeeds
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def check_internet_connectivity(timeout: float = DEFAULT_INTERNET_CHECK_TIMEOUT) -> Tuple[bool, str]:
    """
    Verify actual internet connectivity by testing real endpoints.

    This performs an actual connectivity test rather than relying on
    NetworkManager state, which can report "connected" even when
    there's no real internet access.

    Test order:
    1. JAM backend health endpoint (what actually matters)
    2. Fallback to TCP connection to public DNS servers

    Args:
        timeout: Timeout in seconds for each individual check

    Returns:
        Tuple of (has_internet, check_that_succeeded)
        check_that_succeeded is one of: 'jam_backend', 'cloudflare_dns',
        'google_dns', or 'none'
    """
    # Import here to avoid circular dependency
    from .api import check_api_availability

    # First, try the JAM backend - this is what actually matters
    if check_api_availability(timeout=int(timeout)):
        return True, 'jam_backend'

    # Backend unreachable - could be backend down or no internet
    # Try fallback DNS servers to determine which
    for host, port in FALLBACK_DNS_SERVERS:
        if _check_tcp_connectivity(host, port, timeout):
            # We have internet, just can't reach JAM backend
            # Don't log here - this is normal during routine checks
            check_name = 'cloudflare_dns' if host == '1.1.1.1' else 'google_dns'
            return True, check_name

    # Nothing reachable - no internet
    return False, 'none'


class InternetConnectivityMonitor:
    """
    Monitors internet connectivity with hysteresis to handle flaky connections.

    For environments with poor WiFi (like restaurants), we need to avoid
    rapidly toggling between online/offline states. This class implements:

    - Quick online detection: Single successful check = online
    - Conservative offline detection: Multiple consecutive failures required
    - Configurable thresholds for different use cases

    Usage:
        monitor = InternetConnectivityMonitor()

        # In your event loop or periodic check:
        is_online = monitor.check()
        if monitor.state_changed:
            if is_online:
                print("Internet restored")
            else:
                print("Internet lost")
    """

    def __init__(
        self,
        failures_required_for_offline: int = 5,
        check_interval_seconds: float = 10.0,
        check_timeout_seconds: float = DEFAULT_INTERNET_CHECK_TIMEOUT,
    ):
        """
        Initialize the connectivity monitor.

        Args:
            failures_required_for_offline: Number of consecutive failures
                before declaring offline. Default 5 = ~50-60 seconds of
                failures before triggering offline state.
            check_interval_seconds: Minimum time between checks. Used for
                calculating total time window.
            check_timeout_seconds: Timeout for each connectivity check.
        """
        self.failures_required = failures_required_for_offline
        self.check_interval = check_interval_seconds
        self.check_timeout = check_timeout_seconds

        self._consecutive_failures = 0
        self._is_online = True  # Assume online initially
        self._state_changed = False
        self._last_check_time = 0.0
        self._last_success_method = 'none'

    @property
    def is_online(self) -> bool:
        """Current connectivity state."""
        return self._is_online

    @property
    def state_changed(self) -> bool:
        """True if the last check() call resulted in a state change."""
        return self._state_changed

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failed checks."""
        return self._consecutive_failures

    @property
    def last_success_method(self) -> str:
        """Which check succeeded on last successful connectivity test."""
        return self._last_success_method

    def check(self) -> bool:
        """
        Perform a connectivity check and update state.

        Returns:
            Current online state (after this check)
        """
        self._state_changed = False
        self._last_check_time = time.time()

        has_internet, method = check_internet_connectivity(self.check_timeout)

        if has_internet:
            self._last_success_method = method
            self._consecutive_failures = 0

            if not self._is_online:
                # Transition from offline to online - this IS worth logging
                logger.info(f"Internet connectivity restored (via {method})")
                self._is_online = True
                self._state_changed = True
            # Don't log successful checks - too noisy
        else:
            self._consecutive_failures += 1
            # Only log failures at debug level, and only periodically
            # to avoid filling logs during extended outages
            if self._consecutive_failures <= self.failures_required:
                logger.debug(
                    f"Internet check failed ({self._consecutive_failures}/"
                    f"{self.failures_required} before offline)"
                )

            if self._is_online and self._consecutive_failures >= self.failures_required:
                # Transition from online to offline - this IS worth logging
                logger.warning(
                    f"Internet connectivity lost after {self._consecutive_failures} "
                    f"consecutive failures"
                )
                self._is_online = False
                self._state_changed = True

        return self._is_online

    def reset(self, assume_online: bool = True):
        """
        Reset the monitor state.

        Args:
            assume_online: Initial state to assume after reset
        """
        self._consecutive_failures = 0
        self._is_online = assume_online
        self._state_changed = False
        self._last_success_method = 'none'
