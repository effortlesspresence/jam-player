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
import threading
from urllib.parse import urlparse
import socket
import ssl
import time
import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Callable

from .paths import INTERNET_VERIFIED_FLAG, STATE_MANAGER_ALIVE_FLAG, API_LAST_OK_FLAG, touch_volatile_flag

logger = logging.getLogger(__name__)

# Default timeouts
DEFAULT_NETWORK_WAIT_TIMEOUT = 30  # seconds
DEFAULT_COMMAND_TIMEOUT = 10  # seconds
DEFAULT_INTERNET_CHECK_TIMEOUT = 5  # seconds per check

# Fallback endpoints for internet connectivity verification.
# Used when the JAM backend is unreachable but we need to tell "backend is
# down" apart from "this network has no internet".
#
# These are checked with a VERIFIED TLS handshake on 443 -- NOT a bare TCP
# connect on 53. See _check_tls_connectivity for why that distinction is
# load-bearing (captive portals). Both Cloudflare and Google publish IP SANs
# for these addresses, so the certificate validates against the IP literal
# and no DNS lookup is required (a portal hijacks DNS too).
#
# NOTE: customer firewall whitelists must allow these on TCP 443.
FALLBACK_TLS_HOSTS = [
    ("1.1.1.1", 443),   # Cloudflare (cert has IP SAN 1.1.1.1)
    ("8.8.8.8", 443),   # Google (cert has IP SAN 8.8.8.8)
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
_wifi_cache_lock = threading.Lock()


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


def _find_wifi_profiles_for_ssid(ssid: str) -> List[str]:
    """
    Names of saved WiFi profiles whose SSID is EXACTLY `ssid`.

    Exact, parsed comparison -- never a substring test. The old sweep used
    `ssid in stdout`, so connecting to "Shop" matched (and deleted) the
    profile for "Shop-5G"; dual-band "X"/"X-5G"/"X-Guest" naming made that
    common. nmcli -t escapes ':' inside values as '\\:', so the SSID is taken
    after the FIRST ':' and unescaped; the NAME,TYPE listing is split on the
    LAST ':' because TYPE never contains one. Best-effort: [] on any error.
    """
    names: List[str] = []
    try:
        listing = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
            capture_output=True, text=True, timeout=10
        )
        if listing.returncode != 0:
            return names
        for line in listing.stdout.strip().split('\n'):
            if not line or ':' not in line:
                continue
            raw_name, conn_type = line.rsplit(':', 1)
            if conn_type != '802-11-wireless':
                continue
            name = _unescape_nmcli_field(raw_name)
            ssid_result = subprocess.run(
                ['nmcli', '-t', '-f', '802-11-wireless.ssid', 'connection', 'show', name],
                capture_output=True, text=True, timeout=5
            )
            if ssid_result.returncode != 0 or ':' not in ssid_result.stdout:
                continue
            profile_ssid = _unescape_nmcli_field(
                ssid_result.stdout.strip().split(':', 1)[1]
            )
            if profile_ssid == ssid:
                names.append(name)
    except Exception as e:
        logger.warning(f"Could not enumerate saved profiles for '{ssid}': {e}")
    return names


def _connection_is_activating_or_active(conn_name: str) -> bool:
    """
    True if NetworkManager reports `conn_name` as activating or activated.

    Used when nmcli's CLIENT-side `connection up` timeout fires: NM may still
    be mid-activation (slow DHCP / 802.1X), so the profile must not be thrown
    away. GENERAL.STATE is empty for a profile that is not active at all.
    Fail-safe: if NM cannot be asked, answer True (keep) -- an orphan profile
    is recoverable, a destroyed valid activation is not.
    """
    try:
        r = subprocess.run(
            ['nmcli', '-g', 'GENERAL.STATE', 'connection', 'show', conn_name],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode != 0:
            return True  # could not ask -> keep
        return r.stdout.strip().lower().startswith('activat')  # activating|activated
    except Exception as e:
        logger.debug(f"Could not query activation state of {conn_name}: {e}")
        return True


def _discard_failed_wifi_profile(conn_name: str, keyfile_path: Path) -> None:
    """
    Remove the profile created for a connection attempt that did NOT activate.

    _connect_wifi_secure persists the keyfile (autoconnect=true, with the psk)
    BEFORE it knows whether the credentials work. If activation then fails --
    most commonly a wrong password -- that profile must not be left behind:
    NetworkManager would keep re-attempting the bad password on its own
    (autoconnect) and, because it lives on disk, again on every reboot. That is
    exactly the fielded (7440d2d) "entered a wrong password and the JP kept
    saving and retrying it" bug. Only a profile that actually authenticated is
    worth persisting.

    `nmcli connection delete` is the proper removal (drops it from
    NetworkManager AND deletes its keyfile). If nmcli can't (e.g. NM never
    loaded it), fall back to unlinking the file and reloading. Best-effort:
    never raises into the connect path. Deleting by the per-attempt unique
    conn_name can only ever touch THIS attempt's profile, never a good one.
    """
    try:
        subprocess.run(
            ['nmcli', 'connection', 'delete', conn_name],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        logger.debug(f"nmcli delete of failed profile {conn_name} skipped: {e}")
    try:
        if keyfile_path.exists():
            keyfile_path.unlink()
            subprocess.run(
                ['nmcli', 'connection', 'reload'],
                capture_output=True, text=True, timeout=10
            )
    except Exception as e:
        logger.debug(f"Keyfile cleanup for failed profile {conn_name} skipped: {e}")


def _connect_wifi_secure(ssid: str, password: str, hidden: bool = False) -> subprocess.CompletedProcess:
    """
    Connect to a WiFi network securely without exposing the password in process list.

    Uses a temporary NetworkManager connection file to pass credentials securely.
    The file is created with restricted permissions (0600) and deleted immediately
    after use.

    Args:
        ssid: The WiFi network SSID
        password: The WiFi password

    Returns:
        subprocess.CompletedProcess with the result of the connection attempt
    """
    # Generate a unique connection name
    conn_name = f"jam-wifi-{uuid.uuid4().hex[:8]}"

    # Profiles that already exist for this EXACT SSID. Deliberately NOT deleted
    # here: if this attempt fails (typo'd password, slow network) the old
    # profile may be the device's working connection, and deleting it first is
    # how a single typo used to knock a working player offline -- and left
    # connect_to_wifi's _restore_wifi_connection() pointing at a name that no
    # longer existed. They are removed only AFTER the new profile has actually
    # activated (below). Two profiles for one SSID coexisting briefly is fine:
    # `nmcli connection up <name>` activates by name, so there is no conflict.
    superseded = _find_wifi_profiles_for_ssid(ssid)

    # Create a NetworkManager keyfile (connection profile) with the
    # credentials. This avoids passing the password as a command-line
    # argument.
    #
    # For OPEN networks (no password) the [wifi-security] section must
    # be OMITTED entirely. Writing `key-mgmt=wpa-psk` with an empty psk
    # makes NetworkManager attempt WPA-PSK against an open AP, which
    # fails with "Secrets were required, but not provided" before any
    # association is attempted. This is exactly what produced the
    # "mobile app says 'Invalid password' for a network that has no
    # password" symptom on fielded JPs prior to this fix.
    if password:
        security_section = (
            "\n[wifi-security]\n"
            "key-mgmt=wpa-psk\n"
            f"psk={password}\n"
        )
    else:
        security_section = ""

    # A truly hidden AP never broadcasts its SSID, so NetworkManager will not
    # see it in a scan; it must actively probe by name, which it only does
    # when the profile says hidden=true. Setting this on a network that turns
    # out to be VISIBLE is harmless -- it still connects, the radio just sends
    # a directed probe for the name -- so manual entry can always set it.
    # Omitted for visible-network connects (the default), whose keyfile stays
    # byte-identical to before.
    hidden_section = "hidden=true\n" if hidden else ""

    keyfile_content = f"""[connection]
id={conn_name}
type=wifi
autoconnect=true

[wifi]
ssid={ssid}
mode=infrastructure
{hidden_section}{security_section}
[ipv4]
method=auto

[ipv6]
method=auto
"""

    # NetworkManager connection files must be in /etc/NetworkManager/system-connections/
    nm_connections_dir = Path('/etc/NetworkManager/system-connections')
    keyfile_path = nm_connections_dir / f"{conn_name}.nmconnection"

    try:
        # Write the keyfile with restricted permissions (required by NetworkManager)
        # Use os.open with exclusive flags to avoid race conditions
        fd = os.open(
            str(keyfile_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600  # rw------- (required by NetworkManager)
        )
        try:
            os.write(fd, keyfile_content.encode('utf-8'))
        finally:
            os.close(fd)

        # Ensure root ownership (NetworkManager requirement)
        os.chown(str(keyfile_path), 0, 0)

        logger.info(f"Created secure connection profile for SSID: {ssid}")

        # Tell NetworkManager to reload connection files
        reload_result = subprocess.run(
            ['nmcli', 'connection', 'reload'],
            capture_output=True,
            text=True,
            timeout=10
        )

        if reload_result.returncode != 0:
            logger.warning(f"nmcli connection reload warning: {reload_result.stderr}")

        # Activate the connection
        logger.info(f"Activating connection: {conn_name}")
        try:
            result = subprocess.run(
                ['nmcli', 'connection', 'up', conn_name],
                capture_output=True,
                text=True,
                timeout=30
            )
        except subprocess.TimeoutExpired as e:
            # This is nmcli's CLIENT-side timeout, not a verdict from NM. On a
            # slow network (802.1X/MAB ports, DHCP snooping, some ISP gateways)
            # NM may still be activating -- its own ceiling is ~90 s -- with
            # perfectly good credentials: a wrong password fails FAST with
            # "Secrets were required"; it does not time out. Discarding here
            # would abort a valid activation and turn a slow network into a
            # permanently failing one (a regression vs 7440d2d). Keep the
            # profile while NM says it is activating/active; discard only when
            # NM positively says it is not; keep if we cannot even ask.
            if _connection_is_activating_or_active(conn_name):
                logger.warning(
                    f"nmcli timed out after 30 s but {conn_name} is still activating in "
                    f"NetworkManager; keeping the profile so the connection can complete"
                )
            else:
                logger.info(f"Activation timed out and is not in progress; discarding {conn_name}")
                _discard_failed_wifi_profile(conn_name, keyfile_path)
            return subprocess.CompletedProcess(
                args=e.cmd, returncode=1, stdout='', stderr=str(e)
            )

        logger.info(f"Connection result: returncode={result.returncode}")
        if result.stdout.strip():
            logger.info(f"stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            # Sanitize stderr to remove any password-related info
            stderr_sanitized = result.stderr.strip()
            if 'psk' in stderr_sanitized.lower() or 'password' in stderr_sanitized.lower():
                logger.info("stderr: [contains sensitive info - redacted]")
            else:
                logger.info(f"stderr: {stderr_sanitized}")

        if result.returncode != 0:
            # Activation failed (wrong password, AP rejected us, ...). Do NOT
            # leave the just-written profile behind: with autoconnect=true NM
            # would keep retrying the bad credentials on its own and on every
            # reboot. Discard it so a failed attempt leaves nothing to retry.
            # The pre-existing profile(s) for this SSID are untouched.
            logger.info(f"Activation failed; discarding connection profile {conn_name}")
            _discard_failed_wifi_profile(conn_name, keyfile_path)
        else:
            # The new profile is live, so any older profile for the same SSID is
            # now redundant (two autoconnect candidates for one network). Remove
            # them only now -- never before we know the new one actually works.
            for old_name in superseded:
                if old_name == conn_name:
                    continue
                try:
                    subprocess.run(
                        ['nmcli', 'connection', 'delete', old_name],
                        capture_output=True, text=True, timeout=10
                    )
                    logger.info(f"Removed superseded profile for '{ssid}': {old_name}")
                except Exception as e:
                    logger.warning(f"Could not remove superseded profile {old_name}: {e}")

        return result

    except Exception as e:
        logger.error(f"Error creating connection profile: {e}")
        # Clean up on error -- same discard as a failed activation, so NM
        # never keeps an in-memory profile whose keyfile we removed.
        _discard_failed_wifi_profile(conn_name, keyfile_path)
        # Return a fake failed result
        return subprocess.CompletedProcess(
            args=['nmcli'],
            returncode=1,
            stdout='',
            stderr=str(e)
        )


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



# ----------------------------------------------------------------------------
# Autoconnect priority
# ----------------------------------------------------------------------------
# NetworkManager picks among saved WiFi profiles by connection.autoconnect-
# priority (higher wins; range -999..999). We never used to set it, so a
# player with several known networks reconnected to whichever NM preferred,
# not the one the user last chose. Now: the network the user most recently
# connected to is ALWAYS the top autoconnect candidate, and the others keep
# their relative order beneath it.

# Stay well inside NM's documented range so we can always add 1 on top.
_AUTOCONNECT_PRIORITY_CEILING = 900


def _unescape_nmcli_field(value: str) -> str:
    """nmcli -t escapes ':' and '\\' in field values with a backslash."""
    return value.replace('\\:', ':').replace('\\\\', '\\')


def _split_nmcli_fields(line: str, count: int) -> Optional[List[str]]:
    """Split a `nmcli -t` line on UNESCAPED colons into exactly `count` fields."""
    fields, cur, esc = [], '', False
    for ch in line:
        if esc:
            cur += ch; esc = False
        elif ch == '\\':
            cur += ch; esc = True
        elif ch == ':':
            fields.append(cur); cur = ''
        else:
            cur += ch
    fields.append(cur)
    if len(fields) != count:
        return None
    return [_unescape_nmcli_field(f) for f in fields]


def _list_wifi_profile_priorities() -> List[Tuple[str, int]]:
    """
    All saved 802-11-wireless profiles with their autoconnect priority.

    ONE nmcli call for the whole list (it used to be one per profile, each
    with a 10 s timeout, on the thread that reports "connected" to the app;
    with many saved networks that pushed past the app's 60 s wait).

    Returns:
        List of (connection_name, priority). Unparseable priorities read as 0.
    """
    profiles: List[Tuple[str, int]] = []
    try:
        listing = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,AUTOCONNECT-PRIORITY', 'connection', 'show'],
            capture_output=True, text=True, timeout=10
        )
        if listing.returncode != 0:
            return profiles
        for line in listing.stdout.strip().split('\n'):
            if not line:
                continue
            fields = _split_nmcli_fields(line, 3)
            if not fields:
                continue
            name, ctype, prio = fields
            if ctype != '802-11-wireless':
                continue
            try:
                priority = int(prio or 0)
            except ValueError:
                priority = 0
            profiles.append((name, priority))
    except Exception as e:
        logger.warning(f"Could not list WiFi profile priorities: {e}")
    return profiles


def _set_wifi_profile_priority(connection_name: str, priority: int) -> bool:
    """Persist connection.autoconnect-priority on one saved profile."""
    try:
        result = subprocess.run(
            ['nmcli', 'connection', 'modify', connection_name,
             'connection.autoconnect-priority', str(priority)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            logger.warning(
                f"Could not set priority {priority} on '{connection_name}': "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"Could not set priority on '{connection_name}': {e}")
        return False


def promote_wifi_connection_priority(connection_name: str) -> None:
    """
    Make `connection_name` the top autoconnect candidate among saved WiFi
    profiles, preserving the relative order of all the others.

    Called after every successful user-driven connect (new network, saved
    network, or re-selecting the current one), so "the network you chose
    last" is always "the network the player comes back to on its own".

    Best-effort by design: the device is already connected by the time this
    runs, and a priority we failed to write must never turn a successful
    connect into a reported failure. Every problem is logged, none raised.
    """
    if not connection_name:
        return
    try:
        profiles = _list_wifi_profile_priorities()
        others = [(n, p) for n, p in profiles if n != connection_name]
        top = max((p for _, p in others), default=0)

        if top + 1 <= _AUTOCONNECT_PRIORITY_CEILING:
            # Common case: one write, everyone else untouched.
            if _set_wifi_profile_priority(connection_name, top + 1):
                logger.info(
                    f"WiFi autoconnect priority: '{connection_name}' -> {top + 1} "
                    f"(top of {len(profiles)} saved profile(s))"
                )
            return

        # Ceiling reached after many switches: renumber compactly, keeping
        # the existing order, then put the chosen profile above them all.
        logger.info("WiFi autoconnect priorities at ceiling - renumbering saved profiles")
        ordered = sorted(others, key=lambda np: np[1])
        for index, (name, _) in enumerate(ordered):
            _set_wifi_profile_priority(name, index)
        _set_wifi_profile_priority(connection_name, len(ordered))
        logger.info(
            f"WiFi autoconnect priority: '{connection_name}' -> {len(ordered)} "
            f"after renumbering {len(ordered)} other profile(s)"
        )
    except Exception as e:
        # Never let priority bookkeeping affect the connect result.
        logger.warning(f"WiFi priority promotion skipped for '{connection_name}': {e}")


def _promote_active_wifi_connection() -> None:
    """Promote whichever WiFi profile is active right now (name resolved live)."""
    try:
        active = _get_active_wifi_connection()
        if active and active.get('name'):
            promote_wifi_connection_priority(active['name'])
        else:
            logger.warning("Connected, but no active WiFi profile found to promote")
    except Exception as e:
        logger.warning(f"Could not resolve active WiFi profile for promotion: {e}")


def _promote_in_background(target: Callable[[], None]) -> None:
    """
    Run a priority promotion on its own daemon thread.

    The connect functions return "connected" to the BLE handler, which is
    what the app is polling for with a 60 s deadline; the promotion's nmcli
    calls must not sit in front of that. The device is already connected
    when this runs, so nothing waits on the result.
    """
    try:
        threading.Thread(target=target, name='wifi-priority-promotion', daemon=True).start()
    except Exception as e:
        logger.warning(f"Could not start priority promotion thread: {e}")


def connect_to_wifi(ssid: str, password: str, hidden: bool = False) -> Tuple[bool, str]:
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
                # The user still chose it explicitly: make it the top
                # autoconnect candidate so that choice sticks across reboots.
                _promote_in_background(_promote_active_wifi_connection)
                return True, ""
            logger.info(f"Connected to {ssid} but no internet - will attempt reconnect")

        # Log diagnostic info before attempting connection (helps debug failures)
        _log_network_diagnostic_info()

        # Stop comitup hotspot if running - can't use wlan0 for both AP and client mode
        hotspot_stopped = _stop_comitup_hotspot()
        logger.info(f"Hotspot stop result: {hotspot_stopped}")

        # Try to connect using nmcli with secure password handling
        # We use a connection file to avoid exposing password in process list
        logger.info(f"Connecting to WiFi network: {ssid}")
        result = _connect_wifi_secure(ssid, password, hidden=hidden)

        if result.returncode == 0:
            logger.info(f"Successfully connected to {ssid}")
            # Newly connected network becomes the top autoconnect candidate.
            _promote_in_background(_promote_active_wifi_connection)
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


def get_saved_wifi_networks() -> List[Dict[str, str]]:
    """
    Get list of WiFi networks saved in NetworkManager.

    These are networks the device has previously connected to and remembers.
    Users can reconnect to these without entering a password.

    Returns:
        List of dicts with keys: 'ssid', 'name' (connection profile name)
    """
    try:
        # Get all saved connections
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            logger.warning(f"Failed to list connections: {result.stderr}")
            return []

        saved_networks = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':')
            if len(parts) >= 2 and parts[1] == '802-11-wireless':
                connection_name = parts[0]
                # Get the SSID for this connection
                ssid_result = subprocess.run(
                    ['nmcli', '-t', '-f', '802-11-wireless.ssid',
                     'connection', 'show', connection_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if ssid_result.returncode == 0:
                    ssid_line = ssid_result.stdout.strip()
                    # Format is "802-11-wireless.ssid:MyNetwork"
                    if ':' in ssid_line:
                        ssid = ssid_line.split(':', 1)[1]
                        if ssid:
                            saved_networks.append({
                                'ssid': ssid,
                                'name': connection_name
                            })

        logger.info(f"Found {len(saved_networks)} saved WiFi networks")
        return saved_networks

    except subprocess.TimeoutExpired:
        logger.error("Timeout getting saved networks")
        return []
    except Exception as e:
        logger.error(f"Error getting saved networks: {e}")
        return []


def connect_to_saved_wifi(connection_name: str) -> Tuple[bool, str]:
    """
    Connect to a saved WiFi network by its NetworkManager profile name.

    This is used when the user wants to reconnect to a previously saved
    network without entering the password again.

    If already connected to a working network and the new connection attempt fails,
    we restore the previous connection to avoid leaving the device offline.

    Args:
        connection_name: The NetworkManager connection profile name

    Returns:
        Tuple of (success, error_message)
    """
    try:
        # Save current connection state before attempting new connection
        previous_connection = _get_active_wifi_connection()
        if previous_connection:
            logger.info(f"Currently connected to: {previous_connection['name']} (will restore if new connection fails)")

        # Stop comitup hotspot if running
        _stop_comitup_hotspot()

        logger.info(f"Connecting to saved network: {connection_name}")

        result = subprocess.run(
            ['nmcli', 'connection', 'up', connection_name],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            logger.info(f"Connected to saved network: {connection_name}")
            # Re-selecting a saved network promotes it to the top, so the
            # player reconnects to the user's latest choice on its own.
            _promote_in_background(lambda: promote_wifi_connection_priority(connection_name))
            return True, ""
        else:
            error_msg = result.stderr.strip() or "Connection failed"
            logger.error(f"Failed to connect to saved network: {error_msg}")

            # If we had a previous working connection, try to restore it
            if previous_connection and previous_connection['name'] != connection_name:
                logger.info(f"Restoring previous connection to: {previous_connection['name']}")
                _restore_wifi_connection(previous_connection['name'])

            return False, error_msg

    except subprocess.TimeoutExpired:
        logger.error("Timeout connecting to saved network")
        # Try to restore previous connection on timeout
        if previous_connection and previous_connection['name'] != connection_name:
            _restore_wifi_connection(previous_connection['name'])
        return False, "timeout"
    except Exception as e:
        logger.error(f"Error connecting to saved network: {e}")
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


def get_reported_network_status() -> Dict[str, Optional[str]]:
    """
    Summarise the network this device is actually using, for the backend.

    Returns {'connectionType': 'ethernet' | 'wifi' | 'other',
             'ssid': <str> | None}.

    Ethernet wins when both are active: it carries the default route on our
    devices (lower metric), so it is what the device is really using for the
    internet, and it has no SSID. Only WiFi carries an SSID. 'other' covers
    anything else (a tether, a bridge) so the field is always meaningful.

    Returns connectionType 'other' with ssid None when nothing is active --
    but note the reporter only runs while the device is online (it is a signed
    HTTP call), so in practice the backend stores the last ONLINE network and
    treats it as "last known" once the device stops reporting.
    """
    ethernet_active = False
    wifi_ssid: Optional[str] = None
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'TYPE', 'connection', 'show', '--active'],
            capture_output=True, text=True, timeout=DEFAULT_COMMAND_TIMEOUT
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                t = line.strip().lower()
                if t in ('802-3-ethernet', 'ethernet'):
                    ethernet_active = True
                elif t in ('802-11-wireless', 'wifi'):
                    active = _get_active_wifi_connection()
                    if active:
                        wifi_ssid = active.get('ssid') or None
    except Exception as e:
        logger.warning(f"Could not read network status: {e}")

    if ethernet_active:
        return {'connectionType': 'ethernet', 'ssid': None}
    if wifi_ssid is not None:
        return {'connectionType': 'wifi', 'ssid': wifi_ssid}
    return {'connectionType': 'other', 'ssid': None}


def report_network_status() -> bool:
    """
    Tell the backend which network this device is on (POST /jam-players/network-status).

    The one implementation shared by every caller: jam-heartbeat calls it after
    each successful heartbeat (periodic refresh), and jam-announce calls it once
    right after announce succeeds so the backend has the network from the moment
    the JamPlayer row exists -- without waiting for the first heartbeat.

    Best-effort and non-fatal: only meaningful while the device is online (it is
    a signed HTTP call), swallows every error, and never raises into its caller.
    The backend keeps the last reported value once the device stops reporting,
    which is how "current when online" doubles as "last known when offline".

    Returns True if the report was sent and accepted, False otherwise (callers
    ignore it; the return is for tests).
    """
    try:
        from .api import api_request  # lazy import: avoid a load-time cycle
        status = get_reported_network_status()
        response = api_request(
            method='POST',
            path='/jam-players/network-status',
            body={'connectionType': status['connectionType'], 'ssid': status['ssid']},
            signed=True,
        )
        return bool(response is not None and getattr(response, 'status_code', 500) < 300)
    except Exception as e:
        logger.debug(f"Network-status report skipped: {e}")
        return False


def get_mac_addresses() -> Dict[str, Optional[str]]:
    """
    Read this device's WiFi and ethernet hardware (MAC) addresses.

    Returns {'wifiMac': <str|None>, 'ethernetMac': <str|None>}, uppercased.

    We pin cloned-mac-address=permanent for both interfaces (see
    etc/NetworkManager/conf.d/mac-address-permanent.conf), so the CURRENT
    hwaddr NetworkManager reports IS the permanent one -- no ethtool needed.
    Parsed from `nmcli device show` by interface TYPE rather than a hardcoded
    interface name, so it is robust to wlan0/eth0 vs predictable names. A
    device that has no ethernet (or no wifi) simply reports None for it.
    """
    wifi_mac: Optional[str] = None
    eth_mac: Optional[str] = None
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'GENERAL.TYPE,GENERAL.HWADDR', 'device', 'show'],
            capture_output=True, text=True, timeout=DEFAULT_COMMAND_TIMEOUT
        )
        if result.returncode == 0:
            cur_type = None
            for line in result.stdout.split('\n'):
                # nmcli -t escapes the ':' inside a MAC as '\:'; split only on
                # the FIRST unescaped ':' that separates key from value.
                if line.startswith('GENERAL.TYPE:'):
                    cur_type = line.split(':', 1)[1].strip().lower()
                elif line.startswith('GENERAL.HWADDR:'):
                    hwaddr = line.split(':', 1)[1].strip().replace('\\:', ':').upper()
                    if not hwaddr:
                        continue
                    if cur_type == 'wifi' and wifi_mac is None:
                        wifi_mac = hwaddr
                    elif cur_type == 'ethernet' and eth_mac is None:
                        eth_mac = hwaddr
    except Exception as e:
        logger.warning(f"Could not read MAC addresses: {e}")
    return {'wifiMac': wifi_mac, 'ethernetMac': eth_mac}


def report_mac_addresses() -> bool:
    """
    Report the device's permanent WiFi + ethernet MACs to the backend
    (POST /jam-players/mac-addresses).

    MACs are permanent, so this is NOT reported on an interval. It is called
    once right after announce succeeds and once per boot on the first
    successful heartbeat -- boot-triggered re-assertion that reaches the
    backend reliably (surviving a failed first attempt, a device that pinned
    its MACs in a firmware update, or backend data loss) without polling.

    Best-effort: only meaningful while online, swallows every error, never
    raises into its caller. Skips the call entirely if neither MAC could be
    read. Returns True only when the report was sent and accepted (for tests).
    """
    try:
        macs = get_mac_addresses()
        if not macs['wifiMac'] and not macs['ethernetMac']:
            logger.debug("No MAC addresses readable; skipping report")
            return False
        from .api import api_request  # lazy import: avoid a load-time cycle
        response = api_request(
            method='POST',
            path='/jam-players/mac-addresses',
            body={'wifiMac': macs['wifiMac'], 'ethernetMac': macs['ethernetMac']},
            signed=True,
        )
        return bool(response is not None and getattr(response, 'status_code', 500) < 300)
    except Exception as e:
        logger.debug(f"MAC-address report skipped: {e}")
        return False

def _check_tls_connectivity(host: str, port: int, timeout: float) -> bool:
    """
    Prove real end-to-end internet by completing a TLS handshake with FULL
    certificate verification against `host`.

    WHY NOT A BARE TCP CONNECT (the bug this replaces):
    A captive portal transparently accepts TCP to *any* address, so
    socket.connect_ex(('1.1.1.1', 53)) returns 0 against the portal's
    interceptor and reports "internet works" when it does not. That produced
    a fielded failure where a device on a portal network wrote
    .internet_verified, was then registered, and -- being "online AND
    registered" -- stopped BLE provisioning: no internet, no BLE, no
    Tailscale, recoverable only by physically touching the device.
    Reproduced on a Starbucks portal: backend HTTPS check correctly failed
    (curl exit 7) while `nc -z 1.1.1.1 53` returned 0.

    A portal cannot forge a certificate that validates for 1.1.1.1 or
    8.8.8.8 -- no CA will issue one -- so a verified handshake is the
    cheapest proof we actually reached the real host.

    We verify against the IP literal itself (both providers publish IP SANs
    for their resolver addresses), so this needs NO DNS lookup, which
    matters because portals hijack DNS as well.

    Args:
        host: IP address of the fallback host
        port: Port number (443)
        timeout: Timeout in seconds, covering connect AND handshake

    Returns:
        True only if the TLS handshake completed and the certificate
        validated for `host`.
    """
    context = ssl.create_default_context()
    # Explicit rather than implicit: these are the two properties that make
    # this check portal-proof, so never let a future refactor weaken them.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    try:
        with socket.create_connection((host, port), timeout=timeout) as raw_sock:
            # server_hostname as an IP literal: Python skips SNI (per RFC 6066)
            # and validates against the certificate's IP SANs instead.
            with context.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                # getpeercert() is non-empty only after a validated handshake.
                return bool(tls_sock.getpeercert())
    except (ssl.SSLError, ssl.CertificateError, OSError):
        # Portal interception lands here (cert mismatch / handshake failure),
        # as does a genuinely offline network. Both mean "no internet".
        return False
    except Exception as e:
        logger.debug(f"Unexpected error in TLS connectivity check to {host}: {e}")
        return False


# How old the state manager's liveness stamp may be before its flag is
# treated as unknown. The manager stamps every 15 s (its check interval); 60 s allows a slow tick
# (a connectivity check can take ~15 s) plus a service restart without a
# false "unknown", and bounds how long a dead manager can freeze the answer.
# API health is a separate question from connectivity and is answered by a
# different signal: every SUCCESSFUL signed API call (heartbeat every 5 min,
# announce) stamps API_LAST_OK_FLAG. Anything that must know "is our API
# actually serving" (not just "is our edge reachable") reads the stamp. 15 min
# = three missed heartbeats before we stop trusting it.
API_HEALTH_MAX_AGE_SECONDS = 15 * 60


def stamp_api_ok() -> None:
    """Record that a real, signed API call just succeeded. Best-effort."""
    touch_volatile_flag(API_LAST_OK_FLAG)


def api_recently_ok(max_age: float = API_HEALTH_MAX_AGE_SECONDS) -> Optional[bool]:
    """
    True  -> a signed API call succeeded within max_age (API is serving).
    False -> the last success is older than max_age (API probably down).
    None  -> no evidence this boot, or the stamp is unreadable.
    Callers deciding to REDUCE reachability (e.g. stop BLE) must require True.
    """
    try:
        age = time.time() - API_LAST_OK_FLAG.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None
    return age <= max_age


CONNECTIVITY_STATE_MAX_AGE_SECONDS = 60


def connectivity_state_is_fresh(max_age: float = CONNECTIVITY_STATE_MAX_AGE_SECONDS) -> bool:
    """True while jam-ble-state-manager has stamped its liveness within max_age."""
    try:
        age = time.time() - STATE_MANAGER_ALIVE_FLAG.stat().st_mtime
        return age <= max_age
    except FileNotFoundError:
        return False  # never stamped this boot (manager not up yet, or not running)
    except Exception:
        return False


def is_internet_verified(unreadable_means: bool = False) -> bool:
    """
    Do we have VERIFIED internet right now? Cheap and non-blocking.

    This is the one place that reads the .internet_verified flag, which
    jam-ble-state-manager owns: it clears it at the start of every boot and
    writes it only once check_internet_connectivity() has actually passed.
    Every other service asks this function instead of probing the network
    itself, so "online" means the same thing everywhere.

    THE FLAG IS A CACHE, AND IT LAGS. Going online it is written on the first
    passing check, BEFORE the manager restarts the services that depend on
    it, so a fresh "online" is never missed. Going offline it can linger for
    roughly 20 seconds (link drop) to two minutes (link up, internet dead,
    dead resolvers) while the manager collects two consecutive failures (INTERNET_CHECK_FAILURES_FOR_OFFLINE).
    Callers must tolerate acting on a stale "online" for that long; none of
    them should treat this as a real-time probe.

    THE FLAG CAN ALSO BE FROZEN. It is maintained by one process; if that
    process dies or wedges, the flag stops changing. The manager therefore
    stamps its liveness every tick, and a stale stamp makes the answer
    "unknown" here rather than a confident stale value.

    Args:
        unreadable_means: the answer for "unknown" -- the flag cannot be read
            (I/O error) OR nobody is maintaining it (stale liveness stamp).
            Callers choose the safe direction for THEM: the display and the
            BLE Device Info treat unknown as offline (the user is steered to
            WiFi setup); the oneshot gates treat unknown as online so a dead
            manager can never silently disable a service. Default False.
    """
    if not connectivity_state_is_fresh():
        logger.debug("Connectivity state not being maintained (state manager stamp stale); answering 'unknown'")
        return unreadable_means
    try:
        return INTERNET_VERIFIED_FLAG.exists()
    except Exception as e:
        logger.warning(f"Could not read internet-verified flag ({e}); assuming {'online' if unreadable_means else 'offline'}")
        return unreadable_means


def device_is_offline() -> bool:
    """
    The oneshot services' gate: `not is_internet_verified(...)`, failing OPEN.

    A per-minute timer or a Restart=on-failure unit that runs its whole retry
    ladder while offline writes a burst of WARNING/ERROR lines to the SD card
    on every run. Those services exit quietly when this is True. If the flag
    is unreadable this returns False ("not known to be offline") so the
    service still runs: a broken flag must never disable anything.
    """
    return not is_internet_verified(unreadable_means=True)


# How long the connect flow will wait for chrony to step a stale clock before
# classifying connectivity. Zero cost when already synced. Budget: the mobile
# apps poll for a terminal status for 60 s; nmcli up (<=30 s) + this wait +
# classification (<=~18 s when everything fails) stays inside that except in
# the pathological slow-DHCP-AND-stale-clock-AND-all-probes-failing case.
CLOCK_SYNC_WAIT_SECONDS = 15


def _clock_is_synced() -> bool:
    """chrony reports 'Leap status: Normal'. Lazy import: common.system pulls in
    sdnotify at load, which would break this module's stdlib-only import."""
    try:
        from .system import check_chrony_sync
        return bool(check_chrony_sync())
    except Exception:
        return False


def wait_for_clock_sync(max_wait: int = CLOCK_SYNC_WAIT_SECONDS) -> bool:
    """
    Give chrony a bounded chance to step the clock; return whether it is synced.

    Why: every connectivity probe is a VERIFIED TLS handshake, and validity is
    judged against the device clock. A Pi with no RTC that sat boxed for
    months boots with a months-old fake-hwclock time, so certificates issued
    since then read "not yet valid" and EVERY probe fails -- a perfectly good
    network gets classified 'none'. Waiting a few seconds for chrony's first
    step turns that into an accurate verdict. Returns immediately (no cost)
    when the clock is already synced.
    """
    if _clock_is_synced():
        return True
    try:  # nudge chrony to measure now rather than on its own schedule
        subprocess.run(['chronyc', 'burst', '4/4'], capture_output=True, text=True, timeout=5)
    except Exception:
        pass
    for _ in range(int(max_wait)):
        time.sleep(1)
        if _clock_is_synced():
            return True
    return False


def none_verdict_blames_wifi(clock_synced: bool) -> Tuple[bool, str]:
    """
    May a 'none' classification be blamed on the WiFi network itself?

    Forgetting a profile is the one outcome the customer cannot undo from the
    app, so it needs corroboration. Two ways the verdict is NOT about the WiFi:
      - the clock is not NTP-synced: TLS verification cannot be trusted, the
        failure may be entirely the device's own stale time;
      - ethernet holds the default route: the probes went out the cable, so a
        dead ethernet uplink (WAN down, isolated VLAN) fails them, not the WiFi.
    Fail-safe: if the route cannot be determined, do not blame the WiFi.
    Returns (blame_wifi, reason_not_to).
    """
    if not clock_synced:
        return False, 'clock is not NTP-synced, so TLS verification failures may be the clock, not the network'
    try:
        if get_reported_network_status().get('connectionType') == 'ethernet':
            return False, 'ethernet holds the default route, so the probes never tested the WiFi path'
    except Exception as e:
        return False, f'could not determine the default route ({e})'
    return True, ''


def forget_active_wifi_connection() -> bool:
    """
    Delete the profile of whatever WiFi network is active right now.

    Used when a network associated but has no usable internet (captive
    portal, firewall, dead uplink): we do not want the device to keep the
    profile and auto-reconnect to a network it can never work on. After this
    the device has no active WiFi and falls back to offline -- BLE provisioning
    stays up so the user can pick another network. Best-effort; never raises.

    Returns:
        True if a profile was deleted, False otherwise.
    """
    try:
        active = _get_active_wifi_connection()
        if not active or not active.get('name'):
            return False
        name = active['name']
        result = subprocess.run(
            ['nmcli', 'connection', 'delete', name],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            logger.info(f"Forgot unusable WiFi profile: {name}")
            return True
        logger.warning(f"Could not delete WiFi profile {name}: {result.stderr.strip()}")
        return False
    except Exception as e:
        logger.warning(f"Error forgetting active WiFi profile: {e}")
        return False


def _parse_backend_endpoint(base_url: str) -> Optional[Tuple[str, int]]:
    """(hostname, port) from an API base URL; None if it has no hostname. Pure."""
    parsed = urlparse(base_url)
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or 443


def _backend_endpoint() -> Optional[Tuple[str, int]]:
    """(hostname, port) of the configured API base URL, or None if unparseable.
    Lazy import: common.api imports this module."""
    try:
        from .api import get_api_base_url
        return _parse_backend_endpoint(get_api_base_url())
    except Exception as e:
        logger.debug(f"Could not determine backend endpoint: {e}")
        return None


def check_internet_connectivity(timeout: float = DEFAULT_INTERNET_CHECK_TIMEOUT) -> Tuple[bool, str]:
    """
    Verify actual internet connectivity by testing real endpoints.

    This performs an actual connectivity test rather than relying on
    NetworkManager state, which can report "connected" even when
    there's no real internet access.

    EVERY check here must be portal-proof: a captive-portal network satisfies
    naive reachability tests (it answers TCP, DNS, and plain HTTP for any
    destination) while routing nothing. Both checks below therefore require a
    verified TLS peer -- something a portal cannot fake.

    Test order:
    1. Verified TLS handshake to OUR backend host (proves DNS + a network
       path to our edge + our real certificate -- portal-proof). This is
       deliberately NOT an HTTP request: this function runs every 15 s on
       every player, and a GET /jam-players/health here was ~92% of all API
       Gateway requests the fleet made. A handshake that sends no request is
       not an API call. It also does NOT prove the API behind the edge is
       serving -- that is a different question, answered by api_recently_ok()
       from the heartbeat's signed calls. classify_connectivity() keeps the
       real HTTP check because it is event-driven and needs API health.
    2. Fallback: verified TLS handshake to public resolver IPs on 443,
       which distinguishes "our edge is unreachable" from "no internet"

    Args:
        timeout: Timeout in seconds for each individual check

    Returns:
        Tuple of (has_internet, check_that_succeeded)
        check_that_succeeded is one of: 'jam_backend', 'cloudflare_tls',
        'google_tls', or 'none'
    """
    # First, our own backend edge: a VERIFIED TLS handshake (cert must chain
    # and match our hostname), so a captive portal cannot satisfy it. No HTTP
    # request is sent -- see the docstring for why that matters.
    backend = _backend_endpoint()
    if backend is not None and _check_tls_connectivity(backend[0], backend[1], timeout):
        return True, 'jam_backend'

    # Backend unreachable - could be backend down or no internet.
    # Verified TLS to a public resolver tells us which.
    for host, port in FALLBACK_TLS_HOSTS:
        if _check_tls_connectivity(host, port, timeout):
            # We have real internet, just can't reach the JAM backend.
            # Don't log here - this is normal during routine checks.
            check_name = 'cloudflare_tls' if host == '1.1.1.1' else 'google_tls'
            return True, check_name

    # Nothing verifiable - no internet (or a captive portal, which is
    # operationally the same thing for us).
    return False, 'none'


def classify_connectivity(
    attempts: int = 3,
    delay: float = 3.0,
    timeout: float = 3.0,
) -> str:
    """
    Classify a just-connected network into what actually matters for setup.

    Returns one of:
      'backend'       -- our backend is reachable over verified TLS. Fully
                         usable; setup can proceed.
      'internet_only' -- the public internet answers (verified TLS to
                         Cloudflare/Google) but our backend does NOT. Either a
                         customer firewall blocking our servers, or one of our
                         own outages -- the device cannot tell which, so the
                         caller must NOT forget the profile or treat it as a
                         permanent dead network.
      'none'          -- nothing verifiable answers: captive portal, dead
                         uplink, or a network that firewalls everything we
                         probe. Genuinely unusable.

    Each phase RETRIES (attempts, delay apart) so a settling connection right
    after association is never misclassified: a good-backend network passes
    the backend phase on the first probe (<1s) and pays no retry cost; only a
    genuinely blocked/absent path exhausts the retries. Backend is checked
    first and in full BEFORE concluding 'internet_only', so a single transient
    backend miss on an otherwise-good network never gets mislabelled a
    firewall. Worst case stays under the app's ~60s status-poll window.
    """
    from .api import check_api_availability
    # Phase 1: our backend, retried. This is the only endpoint that lets a
    # JAM Player actually be set up, so it is the success criterion.
    for i in range(max(1, attempts)):
        if check_api_availability(timeout=int(timeout)):
            return 'backend'
        if i < attempts - 1:
            time.sleep(delay)
    # Phase 2: backend unreachable after retries. Is there ANY real internet?
    # One verified-TLS pass to each public resolver is enough to tell
    # "firewalled/our-outage" (internet works) from "no internet at all".
    for host, port in FALLBACK_TLS_HOSTS:
        if _check_tls_connectivity(host, port, timeout):
            return 'internet_only'
    return 'none'


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
