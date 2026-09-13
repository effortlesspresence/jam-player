#!/usr/bin/env python3
"""
JAM BLE State Manager Service

Controls the BLE provisioning service based on actual internet connectivity.
Uses a two-tier approach for reliability:

1. NetworkManager D-Bus signals for quick disconnect detection
2. Actual internet connectivity verification (JAM backend + DNS fallbacks)

Overriding both: BLE provisioning is kept up UNCONDITIONALLY for the first
15 minutes after every boot (BLE_BOOT_RECOVERY_WINDOW_SECONDS), so a player
stranded on a captive-portal / firewalled network can always be rescued by a
power-cycle + the setup app. See docs/BLE_REACHABILITY.md.

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

- Going OFFLINE requires 6 consecutive failed checks (~50-60 seconds)
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
import time
from pathlib import Path

# Add services directory to path for common module imports
sys.path.insert(0, str(Path(__file__).parent))

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from common.system import manage_service, seconds_since_boot, unit_active_since_boot_seconds
from common.network import InternetConnectivityMonitor, check_internet_connectivity, api_recently_ok
from common.credentials import is_device_registered
from common.paths import INTERNET_VERIFIED_FLAG, BLE_SESSION_ACTIVE_FLAG, STATE_MANAGER_ALIVE_FLAG, safe_touch, touch_volatile_flag

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

# Internet connectivity check settings.
#
# Tuning rationale (2026-09, after the probe became a free TLS handshake):
#   - 2 failures x 15 s interval = ~15-30 s worst-case offline detection
#     (the first failure starts the count; the flag flips on the 2nd).
#   - Why not faster: bringing BLE back IN PLACE after a WiFi loss is now a
#     convenience, not the guarantee -- every boot advertises BLE for 15 min
#     (BLE_BOOT_RECOVERY_WINDOW_SECONDS), so a power-cycle always recovers a
#     player. The display rides through any WiFi loss regardless
#     (determine_display_mode() checks PLAYING_CONTENT first); this latency
#     only affects the BLE `isConnected` advertisement bit and the rare
#     no-content setup screens.
#   - Why not slower: this same tick stamps STATE_MANAGER_ALIVE_FLAG, and
#     `.internet_verified` readers treat the state as UNKNOWN once that stamp
#     is older than CONNECTIVITY_STATE_MAX_AGE_SECONDS (60 s). 15 s leaves
#     ~2x margin even when every probe times out (~15 s of probe time); 30 s
#     would not. Do NOT raise this without moving the stamp onto the
#     watchdog timer or raising that ceiling.
#   - Transient tolerance is BETTER than the previous 3 x 7 s: a blip had to
#     span ~14 s to flip the flag then; it must span >= 15 s now.
#   - History: 6 x 10 s (~60 s), then 3 x 7 s (~21 s) while the probe was a
#     billed HTTP GET to /jam-players/health. The probe is now a verified TLS
#     handshake (no API call), so the tick is tuned for reachability margins,
#     not for cost.
INTERNET_CHECK_FAILURES_FOR_OFFLINE = 2
INTERNET_CHECK_INTERVAL_SECONDS = 15

# Services we control
BLE_PROVISIONING_SERVICE = 'jam-ble-provisioning.service'
HEARTBEAT_SERVICE = 'jam-heartbeat.service'

# ----------------------------------------------------------------------------
# Post-boot BLE recovery window
# ----------------------------------------------------------------------------
# Design, rationale and the support recovery procedure: docs/BLE_REACHABILITY.md
#
# BLE provisioning stays up for this long after EVERY boot, no matter what
# this service believes about connectivity or registration.
#
# This is a recovery guarantee, not a convenience. Fielded devices have been
# stranded by networks that look up but go nowhere -- captive portals,
# guest networks that firewall our API and Tailscale, uplinks that are
# simply dead. The device concluded it was online, stopped advertising, and
# there was no way left to reach it: not the app (no BLE), not support (no
# Tailscale). The only fix was a replacement unit.
#
# With this window, the fix is: power-cycle the player and connect the app
# within 15 minutes. The window is measured from /proc/uptime, so it cannot
# be defeated by the very connectivity check whose false positives caused
# the stranding, and it cannot be corrupted by the wall clock (which
# fake-hwclock and NTP both step during boot). We deliberately spend 15
# minutes of radio time every boot to make "unreachable" impossible.
BLE_BOOT_RECOVERY_WINDOW_SECONDS = 15 * 60

# The window never closes underneath somebody who is using it. While a phone
# is connected over BLE, or a WiFi connection started from the app is still
# running, the window is held open. Without this, a setup session started at
# minute 14 was cut off mid-connect: the app saw read errors and then its
# own 60-second timeout, and the player could be left half-configured.
#
# There is deliberately NO overall time cap on the hold: a session that is
# genuinely under way is never cut off, whenever it started. Availability
# beats tidiness here -- being unreachable is the failure we are paying to
# avoid. Both signals are self-limiting instead, so the hold cannot become
# permanent:
#   - the BlueZ answer clears itself when the phone disconnects or the link
#     times out, with no cooperation from us;
#   - the WiFi-connect marker is ignored once it is older than this, because
#     a connect attempt is bounded by nmcli's own timeouts (~60 s worst
#     case). A marker older than that means the attempt died without
#     clearing it, not that somebody is still waiting.
BLE_SESSION_FLAG_MAX_AGE_SECONDS = 5 * 60

# Services to restart when connectivity is restored
# These services may have failed/exited during offline period
POST_CONNECTIVITY_SERVICES = [
    'jam-tailscale.service',
    'jam-ws-commands.service',
    'jam-heartbeat.service',
    'jam-announce.service',
    # Oneshots that now exit 0 when offline instead of failing and being
    # restarted by systemd (which cost hundreds of SD-card writes an hour).
    # They need this nudge to run once connectivity is back.
    'jam-installed-version-reporter.service',
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
        # Log the post-boot recovery window closing exactly once per boot
        self._recovery_window_closed_logged = False
        # Log the session hold once rather than on every periodic tick
        self._session_hold_logged = False

        # One-shot gate: trigger jam-update.service the first time a
        # never-registered device sees internet connectivity, so warehouse
        # devices that sit on the shelf for months auto-update before
        # attempting setup. See _maybe_trigger_first_connect_update().
        # In-memory only: resets if this service restarts, which is fine
        # -- on a normal boot where WiFi is already configured,
        # jam-update has already run via systemd's network-online.target
        # gating, so triggering again at worst costs one extra git fetch.
        self._first_connect_update_triggered = False

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
        self._stamp_alive()
        try:
            self._periodic_connectivity_check_body()
        except Exception:
            # PyGObject silently REMOVES a timeout source whose callback
            # raises. One stray exception here (a dying SD card making
            # Path.exists() raise EIO, a D-Bus hiccup) would end this loop
            # for the rest of the boot: offline would never be detected
            # again and BLE never restarted. Log it and keep the loop alive.
            logger.exception("Periodic connectivity check failed; continuing")
        return True  # Keep the timeout repeating

    def _periodic_connectivity_check_body(self) -> None:
        # Re-assert BLE every tick, not only on transitions. A BLE exit that
        # systemd did not restart (StartLimitBurst tripped during a
        # half-installed venv, a manual stop, the exit-0 path) used to stay
        # down until the next connectivity transition or reboot.
        # manage_service() is a no-op when the unit is already in the state
        # we ask for, so this costs one `systemctl is-active` per tick.
        nm_state = self._get_current_state()
        if not self._nm_has_connection(nm_state):
            # NM says disconnected: offline, BLE must run. Nothing else to check.
            manage_service(BLE_PROVISIONING_SERVICE, should_run=True)
            return

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
            # State didn't change, but re-evaluate anyway: registration may
            # have completed via the app, or the post-boot recovery window
            # may have just closed. Either can turn "keep BLE up" into
            # "BLE not needed" with no connectivity transition -- and the
            # reverse (a crashed BLE that must come back) needs the same tick.
            # last_success_method is a @property (a str), not a method. Calling it
            # raised TypeError on every steady-state online tick, which silently
            # skipped the BLE manage step below for the life of an online device.
            should_run = self._should_ble_run(is_online, self._connectivity_monitor.last_success_method)
            if not should_run and not self._recovery_window_closed_logged:
                self._recovery_window_closed_logged = True
                logger.info(
                    f"Post-boot BLE recovery window closed "
                    f"({BLE_BOOT_RECOVERY_WINDOW_SECONDS // 60} min); device is "
                    f"online via our backend and registered - stopping BLE provisioning"
                )
            manage_service(BLE_PROVISIONING_SERVICE, should_run=should_run)
        else:
            manage_service(BLE_PROVISIONING_SERVICE, should_run=True)

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

    def _stamp_alive(self) -> None:
        """
        Prove to the flag's readers that this process is still maintaining it.

        common.network.is_internet_verified() treats a stamp older than its
        freshness limit as "unknown" and lets each caller fail in its own
        safe direction, so a dead or wedged state manager can never freeze
        the fleet's idea of "online" for longer than that limit. tmpfs, so
        this costs no SD-card write.
        """
        if not touch_volatile_flag(STATE_MANAGER_ALIVE_FLAG):
            logger.debug("Could not stamp state-manager liveness")

    def _in_boot_recovery_window(self) -> bool:
        """
        True while the recovery window is open.

        The window runs for BLE_BOOT_RECOVERY_WINDOW_SECONDS from the LATER
        of two moments: power-on, and the moment jam-ble-provisioning most
        recently became active. Measuring from power-on alone let anything
        that delayed BLE's start -- a slow filesystem check, a bluetoothd
        restart, BLE crash-looping into its start limit and being brought
        back -- quietly shorten the guarantee, so a player could come up
        with the window already gone. The guarantee is "fifteen minutes in
        which a phone can reach this player", so it has to count from when
        the player can actually be reached.

        If systemd cannot say when BLE came up, the power-on rule stands on
        its own: unknown must never shrink the window.
        """
        uptime = seconds_since_boot()
        if uptime < BLE_BOOT_RECOVERY_WINDOW_SECONDS:
            return True
        ble_active_since = self._ble_active_since_cached(uptime)
        if ble_active_since is None:
            return False
        return uptime < ble_active_since + BLE_BOOT_RECOVERY_WINDOW_SECONDS

    # systemd is asked at most this often; a fork every periodic tick for the
    # life of the device would be a waste, and BLE's activation time only
    # changes when BLE restarts.
    _BLE_ACTIVE_SINCE_CACHE_SECONDS = 60

    def _ble_active_since_cached(self, uptime: float):
        cached = getattr(self, '_ble_active_since_cache', None)
        if cached is not None and uptime - cached[0] < self._BLE_ACTIVE_SINCE_CACHE_SECONDS:
            return cached[1]
        value = unit_active_since_boot_seconds(BLE_PROVISIONING_SERVICE)
        self._ble_active_since_cache = (uptime, value)
        return value

    def _wifi_setup_in_flight(self) -> bool:
        """
        True while jam-ble-provisioning is running a WiFi connect for the app.

        A marker older than BLE_SESSION_FLAG_MAX_AGE_SECONDS is treated as
        stale rather than active: the attempt that raised it is long over,
        and a marker nobody cleared must not hold the radio open for the
        rest of the boot. Measured against the file's own mtime, which the
        tmpfs filesystem keeps for us.
        """
        try:
            if not BLE_SESSION_ACTIVE_FLAG.exists():
                return False
            age = time.time() - BLE_SESSION_ACTIVE_FLAG.stat().st_mtime
            if age > BLE_SESSION_FLAG_MAX_AGE_SECONDS:
                logger.debug(f"Ignoring stale BLE session marker ({age:.0f}s old)")
                return False
            return True
        except Exception:
            return False

    def _ble_central_connected(self) -> bool:
        """
        True while a phone is connected to our GATT server.

        Asked of BlueZ directly (any org.bluez.Device1 with Connected=true)
        rather than tracked in our own process, so it stays correct across a
        restart of either service. Any failure answers False: a D-Bus problem
        must not be able to hold BLE open indefinitely.
        """
        try:
            manager = dbus.Interface(
                self.bus.get_object('org.bluez', '/'),
                'org.freedesktop.DBus.ObjectManager'
            )
            for _path, interfaces in manager.GetManagedObjects().items():
                device = interfaces.get('org.bluez.Device1')
                if device and bool(device.get('Connected', False)):
                    return True
        except Exception as e:
            logger.debug(f"Could not query BlueZ for connected devices: {e}")
        return False

    def _setup_session_in_progress(self) -> bool:
        """
        Hold the recovery window open for an active setup session.

        No overall deadline: a real session is never cut off, whenever it
        started. Each signal expires on its own instead (see the constants),
        so this cannot become a permanent hold.
        """
        if self._wifi_setup_in_flight():
            return True
        return self._ble_central_connected()

    def _should_ble_run(self, is_online: bool, method: str = 'unknown') -> bool:
        """
        Determine if BLE provisioning should be running.

        BLE should run when:
        - The boot is younger than the recovery window (see
          BLE_BOOT_RECOVERY_WINDOW_SECONDS) -- unconditionally, OR
        - Device is offline (no internet), OR
        - Device is not registered (needs setup)

        BLE should stop only when ALL of these hold:
        - The recovery window has closed
        - Device is online AND registered

        The window check comes FIRST and ignores is_online on purpose: a
        false "online" from a captive portal is exactly the input that
        used to strand devices, so it must not be able to close the window.

        Args:
            is_online: Whether internet connectivity is verified

        Returns:
            True if BLE should be running, False if it should be stopped
        """
        if self._in_boot_recovery_window():
            return True  # Recovery window - always reachable after a power-cycle

        if self._setup_session_in_progress():
            # Somebody is mid-setup right now. Closing the window here would
            # cut the session off at exactly the wrong moment.
            if not self._session_hold_logged:
                self._session_hold_logged = True
                logger.info("Holding BLE provisioning open: a setup session is in progress")
            return True

        if not is_online:
            return True  # No internet - BLE needed for WiFi setup

        if not is_device_registered():
            return True  # Online but not registered - BLE needed for registration

        # "Online" via the Cloudflare/Google TLS fallbacks means the general
        # internet answers but OUR backend did not. A guest network that
        # firewalls api.justamenu.com and Tailscale looks exactly like this,
        # and it is one of the stranding cases this file exists for: the
        # player can reach neither the backend nor support. Treat it as
        # degraded and keep BLE up; only a reachable backend may stop BLE.
        if method != 'jam_backend':
            return True

        # 'jam_backend' means our EDGE answered a verified TLS handshake (network

        # path proven, portal-proof) -- not that the API behind it is serving.

        # The authoritative API-health signal is the heartbeat: a signed call

        # every 5 min that stamps API_LAST_OK_FLAG on success. Stopping BLE

        # reduces reachability, so it needs positive, recent proof the API is

        # up; no evidence or stale evidence keeps BLE running -- exactly what a

        # failing HTTP /health probe used to do, on a stronger signal.

        if api_recently_ok() is not True:

            return True

        return False  # Window closed, registered, edge reachable, API recently served us - BLE not needed

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

    def _maybe_trigger_first_connect_update(self):
        """
        If this is the first time this never-registered device has seen
        internet connectivity, trigger jam-update.service so warehouse
        devices auto-update to latest code before they attempt setup.

        Gated by:
          1. Device is not yet .registered (avoids racing active setup
             on deployed devices that just lost and regained WiFi --
             those already picked up updates via their previous boot's
             jam-update).
          2. We have not already fired this trigger in this process
             lifetime (avoids re-firing on every online-transition).

        jam-update.service is Type=oneshot and idempotent (does nothing
        if /etc/jam/version.txt already matches origin/main), so even
        redundant invocations are safe -- but the gate spares us a git
        fetch on every WiFi hiccup.

        Uses `systemctl start` (not `restart`) so systemd treats an
        already-running or already-finished jam-update as a no-op
        rather than kicking off a second run.
        """
        if self._first_connect_update_triggered:
            return

        if is_device_registered():
            # Deployed device -- updates come via the nightly 3 AM
            # reboot cycle. Triggering jam-update here could race an
            # in-flight setup / WebSocket session / content fetch.
            return

        self._first_connect_update_triggered = True

        import subprocess
        logger.info(
            "First internet connection on an unregistered device -- "
            "triggering jam-update.service so warehouse devices catch "
            "up to latest code before setup"
        )
        try:
            result = subprocess.run(
                ['systemctl', 'start', 'jam-update.service'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info("jam-update.service start triggered")
            else:
                logger.warning(
                    f"systemctl start jam-update returned non-zero: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        except subprocess.TimeoutExpired:
            logger.warning("systemctl start jam-update timed out")
        except Exception as e:
            logger.warning(f"Failed to trigger jam-update: {e}")

    def _apply_ble_state(self, is_online: bool, method: str = "unknown"):
        """
        Apply the correct BLE state based on connectivity and registration.

        Also maintains the INTERNET_VERIFIED_FLAG which jam-ble-provisioning
        reads for fast BLE responses (checking file exists vs slow HTTP check).

        Args:
            is_online: Whether internet connectivity is verified
            method: Which connectivity check succeeded (for logging)
        """
        should_run = self._should_ble_run(is_online, method)

        # Maintain the internet verified flag for jam-ble-provisioning
        # This allows fast BLE reads without doing slow connectivity checks
        if is_online:
            try:
                safe_touch(INTERNET_VERIFIED_FLAG)
            except Exception as e:
                logger.warning(f"Failed to create internet verified flag: {e}")

            # Warehouse-device auto-update: on the first time this
            # never-registered device has internet, kick jam-update so
            # it catches up to latest code before attempting setup.
            # Self-gated to fire at most once per process lifetime and
            # only while .registered is absent.
            self._maybe_trigger_first_connect_update()

            registered = is_device_registered()
            if registered:
                # Registered and online: make sure heartbeat runs regardless of
                # what we decide about BLE (its ConditionPathExists was evaluated
                # at boot, before a BLE registration could have created .registered).
                manage_service(HEARTBEAT_SERVICE, should_run=True)

            if should_run:
                if registered and self._in_boot_recovery_window():
                    logger.info(
                        f"Internet online (via {method}), device registered - keeping BLE "
                        f"provisioning running for the post-boot recovery window"
                    )
                elif registered:
                    logger.info(
                        f"Internet online only via {method} (backend unreachable) - "
                        f"keeping BLE provisioning running (degraded network)"
                    )
                else:
                    logger.info(
                        f"Internet online (via {method}) but device not registered - "
                        f"keeping BLE provisioning running"
                    )
            else:
                logger.info(
                    f"Internet online (via {method}) and device registered - "
                    f"stopping BLE provisioning"
                )
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
        1. The monitor uses hysteresis (requires 6 failures to go offline)
        2. The monitor starts with _is_online=True by default
        3. At startup, we need the ACTUAL connectivity state, not hysteresis
        """
        # .internet_verified is a persistent file that only the OFFLINE
        # path deletes. After a power-cycle onto a captive portal it still
        # says "online" for the 40-90 s these checks take (or forever, if
        # this service dies), and the fielded apps route a registered
        # player that reports isConnected straight to its detail screen --
        # never to WiFi setup. Nothing about the previous boot's network
        # is evidence about this one: clear it before the first check.
        try:
            INTERNET_VERIFIED_FLAG.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Could not clear stale internet-verified flag: {e}")
        # From here on the flag is being maintained: readers may trust "absent".
        self._stamp_alive()

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
            # READY=1 was already sent, so WatchdogSec (90 s) is armed, but the
            # GLib pinger only runs once the main loop does -- after this
            # synchronous method returns. With black-holed resolvers three
            # rounds of DNS + TLS timeouts can exceed 90 s and get us killed.
            # Feed the watchdog by hand before each check.
            sd_notifier.notify("WATCHDOG=1")
            is_online, method = check_internet_connectivity()

            if is_online:
                self._last_connected_state = True
                # Sync the monitor state with reality
                self._connectivity_monitor.reset(assume_online=True)
                self._apply_ble_state(is_online=True, method=method)
                # The post-connectivity oneshots (tailscale, announce,
                # installed-version) exit quietly when they see no verified
                # flag, and at boot they race this check: if they started
                # before the flag existed they have already exited 0 and, with
                # RemainAfterExit=yes, now sit "active (exited)" having done
                # nothing. Nothing else re-runs them until the next
                # offline->online transition or the nightly reboot. Coming up
                # online IS such a transition; run them now. (Same for a
                # restart of this service while the device is online.)
                self._restart_post_connectivity_services()
            else:
                # First check failed - try a couple more times before declaring offline
                logger.info("Initial connectivity check failed - performing additional checks...")

                for i in range(2):  # 2 more checks = 3 total
                    import time
                    time.sleep(2)
                    sd_notifier.notify("WATCHDOG=1")
                    is_online, method = check_internet_connectivity()
                    if is_online:
                        self._last_connected_state = True
                        self._connectivity_monitor.reset(assume_online=True)
                        self._apply_ble_state(is_online=True, method=method)
                        self._restart_post_connectivity_services()  # see above
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
