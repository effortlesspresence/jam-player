#!/usr/bin/env python3
"""
JAM BLE State Manager Service

Controls the BLE provisioning service based on actual internet connectivity.
Uses a two-tier approach for reliability:

1. NetworkManager D-Bus signals for quick disconnect detection
2. Actual internet connectivity verification (JAM backend + DNS fallbacks)

=== What This Service Does ===

1. Monitors NetworkManager state via D-Bus for quick disconnect detection
2. Periodically verifies actual internet connectivity (not just NM state)
3. When internet is VERIFIED: Stops jam-ble-provisioning
4. When internet is LOST: Starts jam-ble-provisioning (allow WiFi setup)

=== Why Not Just Trust NetworkManager? ===

NetworkManager's connectivity state is unreliable:
- Can report CONNECTED_SITE (60) when internet actually works fine
- Can report CONNECTED_GLOBAL (70) when internet is down
- Depends on NM's internal connectivity check which may fail

For Fortune 500 deployments, we need certainty. We verify by actually
reaching the JAM backend (what matters for content updates) or falling
back to public DNS servers (1.1.1.1, 8.8.8.8).

=== Handling Flaky WiFi (Restaurant Environments) ===

These devices are often deployed in restaurants with poor WiFi. To avoid
the BLE provisioning network constantly appearing/disappearing:

- Going OFFLINE requires 5 consecutive failed checks (~50-60 seconds)
- Going ONLINE requires just 1 successful check (immediate response)

This asymmetry prevents BLE from activating during brief WiFi drops while
ensuring quick recovery when WiFi is configured.

=== Connectivity Check Priority ===

1. JAM backend health endpoint (/jam-players/health) - most relevant
2. Cloudflare DNS (1.1.1.1:53) - fallback if backend is down
3. Google DNS (8.8.8.8:53) - second fallback

If backend is unreachable but DNS works, we consider internet "up" since
the backend might just be temporarily down.

=== Service Lifecycle ===

This service runs continuously and must handle:
- Graceful shutdown on SIGTERM
- systemd watchdog pings
- Recovery from D-Bus disconnection
"""

import sys
from pathlib import Path

# Add services directory to path for common module imports
sys.path.insert(0, str(Path(__file__).parent))

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from common.system import manage_service
from common.network import InternetConnectivityMonitor, check_internet_connectivity
from common.credentials import is_device_registered
from common.paths import INTERNET_VERIFIED_FLAG

# ============================================================================
# Logging Configuration
# ============================================================================

from common.logging_config import setup_service_logging, log_service_start

logger = setup_service_logging('jam-ble-state-manager')

# ============================================================================
# systemd Integration
# ============================================================================

from common.system import get_systemd_notifier, setup_signal_handlers, setup_glib_watchdog

sd_notifier = get_systemd_notifier()

# ============================================================================
# Constants
# ============================================================================

# NetworkManager D-Bus details
NM_SERVICE = 'org.freedesktop.NetworkManager'
NM_PATH = '/org/freedesktop/NetworkManager'
NM_INTERFACE = 'org.freedesktop.NetworkManager'
DBUS_PROPS_INTERFACE = 'org.freedesktop.DBus.Properties'

# NetworkManager state values
# We use NM state as a quick first check:
#   - If NM says disconnected (<50), we're definitely offline
#   - If NM says connected (>=50), we verify with actual connectivity test
#
# States:
#   50 = CONNECTED_LOCAL (connected but no default route)
#   60 = CONNECTED_SITE (LAN connectivity, may not have internet)
#   70 = CONNECTED_GLOBAL (full internet verified)
#
# We require actual internet verification because NM's connectivity check
# is unreliable - it can report CONNECTED_SITE even when internet works,
# or CONNECTED_GLOBAL when it doesn't.
NM_STATE_CONNECTED_LOCAL = 50

# Internet connectivity check settings
# These are tuned for restaurant environments with flaky WiFi:
# - 5 failures required before declaring offline
# - 30 second interval when stable = ~2.5 minutes of no connectivity before BLE starts
# - This prevents BLE from activating during brief WiFi drops
# - Longer interval reduces SD card writes and log volume
INTERNET_CHECK_FAILURES_FOR_OFFLINE = 5
INTERNET_CHECK_INTERVAL_SECONDS = 30

# Services we control
BLE_PROVISIONING_SERVICE = 'jam-ble-provisioning.service'
HEARTBEAT_SERVICE = 'jam-heartbeat.service'

# Services to restart when connectivity is restored
# These services may have failed/exited during offline period
POST_CONNECTIVITY_SERVICES = [
    'jam-tailscale.service',
    'jam-ws-commands.service',
    'jam-heartbeat.service',
    'jam-announce.service',
]

# Watchdog interval (seconds)
WATCHDOG_INTERVAL = 30

# Debounce delay (seconds) - wait before acting on state changes
# Prevents rapid start/stop cycles during brief disconnections
DEBOUNCE_DELAY = 3


# ============================================================================
# BLE State Manager
# ============================================================================

class BLEStateManager:
    """
    Manages BLE provisioning service based on internet connectivity.

    Uses a two-tier connectivity check:
    1. NetworkManager D-Bus signals for quick disconnect detection
    2. Actual internet connectivity verification (JAM backend + DNS fallbacks)

    This ensures BLE provisioning only activates when the device truly
    cannot reach the internet, not just when NetworkManager reports a
    lower connectivity state.
    """

    def __init__(self, bus):
        """
        Initialize the state manager.

        Args:
            bus: D-Bus system bus connection
        """
        self.bus = bus
        self.mainloop = None
        self._pending_action = None  # GLib timeout ID for debounced action
        self._last_connected_state = None  # Track state to avoid redundant actions
        self._internet_check_timer = None  # GLib timeout for periodic checks

        # Initialize internet connectivity monitor with conservative settings
        # for flaky restaurant WiFi environments
        self._connectivity_monitor = InternetConnectivityMonitor(
            failures_required_for_offline=INTERNET_CHECK_FAILURES_FOR_OFFLINE,
            check_interval_seconds=INTERNET_CHECK_INTERVAL_SECONDS,
        )

        # Get NetworkManager proxy
        try:
            self.nm_proxy = bus.get_object(NM_SERVICE, NM_PATH)
            self.nm_props = dbus.Interface(self.nm_proxy, DBUS_PROPS_INTERFACE)
            logger.info("Connected to NetworkManager D-Bus interface")
        except dbus.exceptions.DBusException as e:
            logger.error(f"Failed to connect to NetworkManager: {e}")
            raise

    def _get_current_state(self) -> int:
        """
        Get current NetworkManager state.

        Returns:
            NetworkManager state integer (0-70)
        """
        try:
            state = self.nm_props.Get(NM_INTERFACE, 'State')
            return int(state)
        except dbus.exceptions.DBusException as e:
            logger.error(f"Failed to get NetworkManager state: {e}")
            return 0  # Return unknown state on error

    def _nm_has_connection(self, state: int) -> bool:
        """
        Check if NetworkManager reports any network connection.

        This is a quick first check - if NM says disconnected, we're
        definitely offline. If NM says connected, we still need to
        verify with actual connectivity test.

        Args:
            state: NetworkManager state integer

        Returns:
            True if NM reports any level of connectivity (state >= 50)
        """
        return state >= NM_STATE_CONNECTED_LOCAL

    def _on_state_changed(self, state: int):
        """
        Handle NetworkManager state change.

        This is called via D-Bus signal when NetworkManager state changes.
        We use NM state as a quick indicator:
        - If NM says disconnected, trigger immediate offline handling
        - If NM says connected, start/continue internet verification

        Args:
            state: New NetworkManager state
        """
        nm_connected = self._nm_has_connection(state)
        state_name = self._state_to_name(state)

        logger.info(f"NetworkManager state changed: {state} ({state_name})")

        # Cancel any pending debounced action
        if self._pending_action is not None:
            GLib.source_remove(self._pending_action)
            self._pending_action = None

        if not nm_connected:
            # NM says disconnected - we're definitely offline
            # No need to verify, just apply immediately after short debounce
            logger.info("NetworkManager reports disconnected - triggering offline state")
            self._pending_action = GLib.timeout_add_seconds(
                DEBOUNCE_DELAY,
                self._apply_offline_state
            )
        else:
            # NM says connected - verify with actual connectivity test
            # The periodic check will handle this, but trigger one now
            logger.info("NetworkManager reports connected - verifying internet connectivity")
            self._pending_action = GLib.timeout_add_seconds(
                DEBOUNCE_DELAY,
                self._verify_and_apply_state
            )

    def _apply_offline_state(self) -> bool:
        """
        Apply offline state - start BLE provisioning.

        Called when NetworkManager reports disconnected (definite offline).

        Returns:
            False (to stop the GLib timeout from repeating)
        """
        self._pending_action = None

        # Reset connectivity monitor since NM says we're disconnected
        self._connectivity_monitor.reset(assume_online=False)

        if self._last_connected_state is False:
            logger.debug("Already in offline state, skipping action")
            return False

        self._last_connected_state = False
        self._apply_ble_state(is_online=False)
        sd_notifier.notify("STATUS=Internet offline - BLE provisioning started")

        return False

    def _apply_online_state(self) -> bool:
        """
        Apply online state - stop BLE provisioning only if device is registered.

        Uses _apply_ble_state() which in turn uses _should_ble_run().
        Also restarts services that may have failed during offline period.

        Returns:
            False (to stop the GLib timeout from repeating)
        """
        self._pending_action = None

        if self._last_connected_state is True:
            logger.debug("Already in online state, skipping action")
            return False

        self._last_connected_state = True
        method = self._connectivity_monitor.last_success_method

        self._apply_ble_state(is_online=True, method=method)

        # Restart services that may have failed/exited during offline period
        self._restart_post_connectivity_services()

        # Update systemd status
        if self._should_ble_run(is_online=True):
            sd_notifier.notify("STATUS=Online but unregistered - BLE provisioning active")
        else:
            sd_notifier.notify("STATUS=Online and registered - BLE provisioning stopped")

        return False

    def _verify_and_apply_state(self) -> bool:
        """
        Verify actual internet connectivity and apply appropriate state.

        This performs a real connectivity test (JAM backend + DNS fallbacks)
        rather than trusting NetworkManager's state.

        Returns:
            False (to stop the GLib timeout from repeating)
        """
        self._pending_action = None

        is_online = self._connectivity_monitor.check()

        if self._connectivity_monitor.state_changed:
            if is_online:
                self._apply_online_state()
            else:
                logger.info(
                    f"Internet connectivity lost after "
                    f"{self._connectivity_monitor.consecutive_failures} failed checks"
                )
                self._last_connected_state = False
                self._apply_ble_state(is_online=False)
                sd_notifier.notify("STATUS=Internet offline - BLE provisioning started")
        else:
            # State unchanged, just log current status
            if is_online:
                method = self._connectivity_monitor.last_success_method
                registered = is_device_registered()
                if registered:
                    sd_notifier.notify(f"STATUS=Online and registered (via {method})")
                else:
                    sd_notifier.notify(f"STATUS=Online but unregistered (via {method}) - BLE active")
            else:
                failures = self._connectivity_monitor.consecutive_failures
                remaining = INTERNET_CHECK_FAILURES_FOR_OFFLINE - failures
                sd_notifier.notify(
                    f"STATUS=Checking connectivity ({failures} failures, "
                    f"{remaining} more before offline)"
                )

        return False

    def _periodic_connectivity_check(self) -> bool:
        """
        Periodic internet connectivity check.

        This runs every INTERNET_CHECK_INTERVAL_SECONDS to verify
        actual internet connectivity, regardless of NetworkManager state.

        Also re-checks registration status in case it changed (user completed
        registration via mobile app while we were online).

        Returns:
            True (to keep the GLib timeout repeating)
        """
        # Only check if NM thinks we have a connection
        nm_state = self._get_current_state()
        if not self._nm_has_connection(nm_state):
            # NM says disconnected, skip the check
            return True

        is_online = self._connectivity_monitor.check()

        if self._connectivity_monitor.state_changed:
            if is_online:
                self._apply_online_state()
            else:
                logger.warning(
                    f"Internet connectivity lost after "
                    f"{INTERNET_CHECK_FAILURES_FOR_OFFLINE} consecutive failures"
                )
                self._last_connected_state = False
                self._apply_ble_state(is_online=False)
                sd_notifier.notify("STATUS=Internet offline - BLE provisioning started")
        elif is_online:
            # State didn't change, but check if registration status changed
            # This handles the case where device gets registered while online
            should_run = self._should_ble_run(is_online)
            if not should_run:
                # Device is now registered - stop BLE if it's running
                manage_service(BLE_PROVISIONING_SERVICE, should_run=False)

        return True  # Keep the timeout repeating

    def _state_to_name(self, state: int) -> str:
        """Convert NetworkManager state integer to human-readable name."""
        states = {
            0: 'UNKNOWN',
            10: 'ASLEEP',
            20: 'DISCONNECTED',
            30: 'DISCONNECTING',
            40: 'CONNECTING',
            50: 'CONNECTED_LOCAL',
            60: 'CONNECTED_SITE',
            70: 'CONNECTED_GLOBAL',
        }
        return states.get(state, f'UNKNOWN({state})')

    def _on_properties_changed(self, interface, changed_props, invalidated_props):
        """
        D-Bus signal handler for PropertiesChanged.

        NetworkManager emits this signal when any property changes.
        We only care about the 'State' property.

        Args:
            interface: The D-Bus interface that changed
            changed_props: Dict of changed properties
            invalidated_props: List of invalidated property names
        """
        if interface != NM_INTERFACE:
            return

        if 'State' in changed_props:
            state = int(changed_props['State'])
            self._on_state_changed(state)

    def setup_signal_handler(self):
        """
        Register to receive NetworkManager state change signals.

        We use PropertiesChanged signal on the org.freedesktop.DBus.Properties
        interface, which fires whenever any NetworkManager property changes.
        """
        # Subscribe to PropertiesChanged signal from NetworkManager
        self.bus.add_signal_receiver(
            self._on_properties_changed,
            signal_name='PropertiesChanged',
            dbus_interface=DBUS_PROPS_INTERFACE,
            bus_name=NM_SERVICE,
            path=NM_PATH
        )
        logger.info("Subscribed to NetworkManager state change signals")

    def _should_ble_run(self, is_online: bool) -> bool:
        """
        Determine if BLE provisioning should be running.

        BLE should run when:
        - Device is offline (no internet), OR
        - Device is not registered (needs setup)

        BLE should stop when:
        - Device is online AND registered

        Args:
            is_online: Whether internet connectivity is verified

        Returns:
            True if BLE should be running, False if it should be stopped
        """
        if not is_online:
            return True  # No internet - BLE needed for WiFi setup

        if not is_device_registered():
            return True  # Online but not registered - BLE needed for registration

        return False  # Online and registered - BLE not needed

    def _restart_post_connectivity_services(self):
        """
        Restart services that may have failed/exited during offline period.

        Called when connectivity is restored. These services need network
        access and may have exited or failed while the device was offline.
        """
        import subprocess

        logger.info("Restarting post-connectivity services...")

        for service in POST_CONNECTIVITY_SERVICES:
            try:
                result = subprocess.run(
                    ['systemctl', 'restart', service],
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                if result.returncode == 0:
                    logger.info(f"Restarted {service}")
                else:
                    # Not an error - service might not be needed yet
                    # (e.g., jam-announce if already announced)
                    logger.debug(f"Could not restart {service}: {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout restarting {service}")
            except Exception as e:
                logger.warning(f"Error restarting {service}: {e}")

    def _apply_ble_state(self, is_online: bool, method: str = "unknown"):
        """
        Apply the correct BLE state based on connectivity and registration.

        Also maintains the INTERNET_VERIFIED_FLAG which jam-ble-provisioning
        reads for fast BLE responses (checking file exists vs slow HTTP check).

        Args:
            is_online: Whether internet connectivity is verified
            method: Which connectivity check succeeded (for logging)
        """
        should_run = self._should_ble_run(is_online)

        # Maintain the internet verified flag for jam-ble-provisioning
        # This allows fast BLE reads without doing slow connectivity checks
        if is_online:
            try:
                INTERNET_VERIFIED_FLAG.touch()
            except Exception as e:
                logger.warning(f"Failed to create internet verified flag: {e}")

            if should_run:
                logger.info(
                    f"Internet online (via {method}) but device not registered - "
                    f"keeping BLE provisioning running"
                )
            else:
                logger.info(
                    f"Internet online (via {method}) and device registered - "
                    f"stopping BLE provisioning"
                )
                # Device is registered and online - ensure heartbeat service is running
                # This handles the case where device was registered via BLE after boot
                # (the heartbeat service's ConditionPathExists was checked at boot when
                # the .registered flag didn't exist yet, so it needs to be started now)
                manage_service(HEARTBEAT_SERVICE, should_run=True)
        else:
            try:
                INTERNET_VERIFIED_FLAG.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to delete internet verified flag: {e}")

            logger.info("Internet offline - starting BLE provisioning")

        manage_service(BLE_PROVISIONING_SERVICE, should_run=should_run)

    def check_initial_state(self):
        """
        Check and apply the current connectivity state on startup.

        This ensures we're in the correct state when the service starts.
        We perform an actual internet connectivity check rather than
        just trusting NetworkManager.

        NOTE: We use check_internet_connectivity() directly here instead of
        the monitor's check() method because:
        1. The monitor uses hysteresis (requires 5 failures to go offline)
        2. The monitor starts with _is_online=True by default
        3. At startup, we need the ACTUAL connectivity state, not hysteresis
        """
        nm_state = self._get_current_state()
        nm_connected = self._nm_has_connection(nm_state)
        state_name = self._state_to_name(nm_state)
        registered = is_device_registered()

        logger.info(f"Initial NetworkManager state: {nm_state} ({state_name})")
        logger.info(f"Device registration status: {'registered' if registered else 'not registered'}")

        if not nm_connected:
            # NM says disconnected - definitely offline, BLE needed
            self._last_connected_state = False
            self._connectivity_monitor.reset(assume_online=False)
            self._apply_ble_state(is_online=False)
        else:
            # NM says connected - verify with actual connectivity test
            # Use raw check_internet_connectivity() to get actual state (no hysteresis)
            logger.info("NetworkManager reports connected - verifying internet connectivity...")
            is_online, method = check_internet_connectivity()

            if is_online:
                self._last_connected_state = True
                # Sync the monitor state with reality
                self._connectivity_monitor.reset(assume_online=True)
                self._apply_ble_state(is_online=True, method=method)
            else:
                # First check failed - try a couple more times before declaring offline
                logger.info("Initial connectivity check failed - performing additional checks...")

                for i in range(2):  # 2 more checks = 3 total
                    import time
                    time.sleep(2)
                    is_online, method = check_internet_connectivity()
                    if is_online:
                        self._last_connected_state = True
                        self._connectivity_monitor.reset(assume_online=True)
                        self._apply_ble_state(is_online=True, method=method)
                        return

                # Still failing after 3 checks - start BLE but keep checking
                logger.warning("Initial connectivity checks failed - starting BLE provisioning")
                logger.info("Will continue checking and stop BLE when internet is confirmed")
                self._last_connected_state = False
                self._connectivity_monitor.reset(assume_online=False)
                self._apply_ble_state(is_online=False)

    def run(self, mainloop):
        """
        Start monitoring network state.

        Args:
            mainloop: GLib main loop to use
        """
        self.mainloop = mainloop

        # Notify systemd we're ready IMMEDIATELY
        # Initial state check happens asynchronously - don't block service startup
        sd_notifier.notify("READY=1")
        logger.info("BLE state manager service ready, starting initialization...")

        # Setup signal handler for NM state changes
        self.setup_signal_handler()

        # Check and apply initial state
        # This may take several seconds for connectivity checks, but service is already "ready"
        self.check_initial_state()

        # Setup periodic internet connectivity checks
        # This catches cases where NM thinks we're connected but internet is down
        self._internet_check_timer = GLib.timeout_add_seconds(
            INTERNET_CHECK_INTERVAL_SECONDS,
            self._periodic_connectivity_check
        )
        logger.info(
            f"Started periodic internet connectivity checks "
            f"(every {INTERNET_CHECK_INTERVAL_SECONDS}s, "
            f"{INTERNET_CHECK_FAILURES_FOR_OFFLINE} failures required for offline)"
        )

        # Setup watchdog pinging
        setup_glib_watchdog(WATCHDOG_INTERVAL)

        logger.info("BLE state manager fully initialized and monitoring")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point for the BLE state manager service."""
    log_service_start(logger, 'JAM BLE State Manager Service')

    # Initialize D-Bus with GLib main loop
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    # Connect to system bus
    try:
        bus = dbus.SystemBus()
    except dbus.exceptions.DBusException as e:
        logger.error(f"Failed to connect to system D-Bus: {e}")
        sys.exit(1)

    # Create state manager
    try:
        manager = BLEStateManager(bus)
    except Exception as e:
        logger.error(f"Failed to initialize BLE state manager: {e}")
        sys.exit(1)

    # Create main loop
    mainloop = GLib.MainLoop()

    # Setup graceful shutdown
    setup_signal_handlers(mainloop.quit, logger)

    # Start monitoring
    manager.run(mainloop)

    # Run main loop
    try:
        mainloop.run()
    except Exception as e:
        logger.exception(f"Main loop error: {e}")
    finally:
        logger.info("BLE state manager stopped")


if __name__ == '__main__':
    main()
