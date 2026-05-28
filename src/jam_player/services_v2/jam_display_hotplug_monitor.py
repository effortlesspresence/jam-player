#!/usr/bin/env python3
"""
jam-display-hotplug-monitor.service

Long-running daemon that watches HDMI connector state and restarts
lightdm when a previously-disconnected port becomes connected. This
handles the "customer unplugs the TV during the day, then plugs it back
in" case -- on reconnect, Wayfire's existing surfaces are at whatever
mode it had before, and the safest way to pick up the TV's native mode
(4K when available) is to restart the display stack.

Polls /sys/class/drm/card*-HDMI-A-*/status once per second. Lightweight
(just a couple of stat+read syscalls per second).

Safety rails (because this can blackout a customer's display):
  - Never restart lightdm more than once per MIN_RESTART_INTERVAL_SEC.
    Prevents storms from cable wiggles, TV brownouts, etc.
  - Only act on the "disconnected -> connected" transition, not on
    "connected -> connected with different mode" (which would also fire
    a uevent but doesn't require a lightdm restart -- Wayfire handles
    in-place mode changes reasonably).
  - If we crash or the service is restarted, treat the *current* state
    as the baseline (no spurious restart on service startup).

This is best-effort optimization. If this service has a bug and never
fires, the customer experience degrades to "must reboot the Pi to fix
display after HDMI reconnect" -- same as today. So failures here are
non-catastrophic.

Future improvement: switch to netlink uevent subscription for instant
detection instead of 1Hz polling. Polling chosen here for simplicity --
no extra dependency, fewer failure modes.
"""
import subprocess
import sys
import time
from pathlib import Path

import sdnotify

from common.logging_config import setup_service_logging

logger = setup_service_logging("jam-display-hotplug-monitor")
sd_notifier = sdnotify.SystemdNotifier()


# Poll interval. 1s gives the customer prompt recovery on reconnect while
# being negligible CPU.
POLL_INTERVAL_SEC = 1.0

# Minimum time between two lightdm restarts. Set to 150s (2.5 minutes) so
# that a flurry of hotplug events (cable wiggle, brownout, customer
# fidgeting) doesn't slam the display stack with back-to-back restarts.
# 2.5 min is also long enough that a single legitimate
# disconnect/reconnect cycle is fully handled before we consider another.
MIN_RESTART_INTERVAL_SEC = 150.0

# How long a disconnect must persist before the eventual reconnect is
# treated as a "real" event that warrants a lightdm restart. Brief sub-
# second EDID renegotiations (TV CEC handshakes, HDMI mode switches,
# the brief flicker many TVs do when power-saving toggles, etc.) all
# manifest as fast disconnect->reconnect cycles in /sys/class/drm.
# Restarting lightdm for those is both unnecessary AND dangerous --
# fielded JPs running ae53eae have a latent lightdm/Wayfire crash bug
# (g_signal_emit_valist assertion on a NULL class pointer) that fires
# probabilistically on lightdm restart and permanently wedges the
# display stack until manual reboot. Customer-visible failure today is
# that 4-6 EDID flaps per day eventually hit that crash and the JP
# stays dead overnight.
#
# 15 seconds is generous enough to absorb any plausible CEC/EDID
# renegotiation (which complete in <1s in practice) while still
# treating an "I unplugged the cable to move it" disconnect as real.
# A customer powering their TV off for the night will be disconnected
# for hours, far past this threshold.
DISCONNECT_DEBOUNCE_SEC = 15.0

# When lightdm restart fails (systemd reports failure, or "Start
# request repeated too quickly"), the display stack is in a wedged
# state that further restarts will not fix and may make worse. Stop
# trying to restart it for this long.
#
# 5 minutes is the trade-off: well past systemd's own ~10s restart
# counter window (so a fresh attempt won't get rejected as
# "Start request repeated too quickly"), short enough that a customer
# whose TV finally turns on doesn't wait too long to see content, and
# long enough to break out of any retry-storm dynamic between us and
# lightdm.
LIGHTDM_FAILURE_BACKOFF_SEC = 300.0

# How often to force a lightdm restart while in "boot recovery" mode --
# i.e. the system came up headless (jam-display-wait-for-hdmi timed
# out, leaving the HDMI_MISSING_AT_BOOT_FLAG behind) and no HDMI has
# been detected since. Each lightdm restart forces the kernel to
# re-enumerate DRM connectors; on Pi 5 boots that found "card0 with
# Cannot find any crtc or sizes" this is the only known way to get the
# kernel to notice that a TV has come on after boot. 5 minutes is the
# tradeoff: long enough that we're not slamming the display stack if
# the customer truly has no TV attached, short enough that powering
# the TV on in the morning gets the display back within a few minutes.
# Floored by MIN_RESTART_INTERVAL_SEC anyway.
RECOVERY_RESTART_INTERVAL_SEC = 300.0

# systemd watchdog ping interval. Service unit sets WatchdogSec=30 so we
# ping every 10s for headroom.
WATCHDOG_PING_INTERVAL_SEC = 10.0

# DRM connector glob.
HDMI_GLOB = "card*-HDMI-A-*"

# Runtime flag set by jam-display-wait-for-hdmi when it gives up after
# 5 minutes without seeing any HDMI connector. Presence at our startup
# tells us this boot came up "headless" and the normal transition-watch
# path is insufficient -- see RECOVERY_RESTART_INTERVAL_SEC docstring.
HDMI_MISSING_AT_BOOT_FLAG = Path("/run/jam-hdmi-was-missing-at-boot")


def _hdmi_connector_states() -> dict[str, bool]:
    """
    Return a dict mapping HDMI connector name -> connected (bool).

    Resilient to connectors disappearing/appearing between polls (rare,
    but happens on suspend/resume on some hardware).
    """
    states: dict[str, bool] = {}
    for connector_dir in Path("/sys/class/drm").glob(HDMI_GLOB):
        try:
            status = (connector_dir / "status").read_text().strip()
        except OSError:
            continue
        states[connector_dir.name] = status == "connected"
    return states


def _clear_recovery_flag() -> None:
    """
    Best-effort removal of HDMI_MISSING_AT_BOOT_FLAG.

    Called when we've successfully detected HDMI after coming up in
    recovery mode -- the flag served its purpose and shouldn't linger
    across hotplug-monitor restarts. Errors are logged but never raised:
    /run/ is tmpfs so the flag dies on reboot anyway.
    """
    try:
        HDMI_MISSING_AT_BOOT_FLAG.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Could not remove {HDMI_MISSING_AT_BOOT_FLAG}: {e}")


def _restart_lightdm() -> bool:
    """
    Restart lightdm.service via systemctl. Returns True iff lightdm is
    actually running afterward (not just "systemctl accepted the
    command").

    Intentionally synchronous -- we want to know whether the restart
    completed before resuming polling, since racing another restart
    would be bad.

    Why the post-restart `is-active` check: `systemctl restart` exits
    with rc=0 even when the unit immediately crashes after start. On
    fielded JPs we've seen lightdm.service crash 5 times in <1 second
    (g_signal_emit_valist assertion on a NULL class pointer), exhaust
    systemd's restart counter, and end up "failed" -- but the original
    `systemctl restart` call returned 0 because it only verifies the
    job was enqueued, not that the unit reached active. Checking
    is-active after the call lets the caller know to enter backoff
    mode rather than blindly trying again.

    Watchdog discipline: the service unit sets WatchdogSec=30. The two
    subprocess calls below + the sleep can collectively block for up to
    ~32s in the worst case, which is enough to miss a watchdog deadline
    and get SIGABRT'd by systemd. We ping the watchdog between the
    blocking calls to keep systemd happy, and use timeouts well below
    WatchdogSec so any individual call can't blow past the deadline on
    its own. (Without this, we observed the monitor process getting
    SIGABRT'd by systemd 30s after kicking off the restart, and entering
    a death-loop where systemd restarted us, we restarted lightdm
    again, watchdog killed us again, etc.)
    """
    logger.info("Restarting lightdm.service to pick up new HDMI EDID...")
    try:
        result = subprocess.run(
            ["systemctl", "restart", "lightdm.service"],
            timeout=20,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                f"lightdm restart returned rc={result.returncode}: "
                f"{result.stderr.strip()}"
            )
            return False
    except subprocess.TimeoutExpired:
        logger.error("lightdm restart timed out after 20s")
        return False
    except Exception as e:
        logger.error(f"lightdm restart raised: {e}")
        return False

    # Reset the systemd watchdog before the second blocking call -- we
    # may have just spent up to 20s in the restart above.
    sd_notifier.notify("WATCHDOG=1")

    # systemctl returned 0; verify lightdm actually reaches the active
    # state. We poll with retries rather than a single check because:
    #   - lightdm legitimately takes a few seconds to start on a slow
    #     boot or when DRM enumeration is happening.
    #   - "activating" is a normal transient state and shouldn't be
    #     treated as failure.
    #   - A single check at T+2s could false-negative on a healthy
    #     restart, which would put us in the 5-min failure backoff
    #     unnecessarily and block legitimate future restarts.
    # We only report failure if the unit ends up explicitly "failed",
    # or if it never reaches "active" within our total budget (~10s --
    # well under the watchdog reset of 30s we set above).
    is_active_deadline = time.monotonic() + 10.0
    last_state = "unknown"
    while time.monotonic() < is_active_deadline:
        # Keep watchdog happy throughout the polling loop. Cheap call.
        sd_notifier.notify("WATCHDOG=1")
        try:
            check = subprocess.run(
                ["systemctl", "is-active", "lightdm.service"],
                timeout=3,
                capture_output=True,
                text=True,
            )
            last_state = check.stdout.strip()
        except Exception as e:
            logger.warning(f"is-active check raised: {e}")
            last_state = "unknown"

        if last_state == "active":
            logger.info("lightdm.service restarted successfully")
            return True
        if last_state == "failed":
            # Hard failure -- no point waiting further. Either the
            # lightdm/Wayfire crash bug hit, or systemd exhausted its
            # restart counter ("Start request repeated too quickly").
            logger.error(
                "lightdm.service post-restart state is 'failed'. Likely "
                "hit the lightdm/Wayfire NULL class pointer crash. "
                "Caller should enter backoff."
            )
            return False
        # "activating" / "deactivating" / "inactive" / unknown: give it
        # more time. The unit is mid-transition.
        time.sleep(1.0)

    logger.error(
        f"lightdm.service did not reach 'active' within 10s "
        f"(last state: '{last_state}'). Treating as failure so caller "
        f"enters backoff. If lightdm IS actually healthy on this device, "
        f"the next reconnect attempt will retry after the backoff window."
    )
    return False


def main() -> int:
    logger.info("=" * 60)
    logger.info("JAM Display Hotplug Monitor Starting")
    logger.info("=" * 60)
    logger.info(
        f"Poll interval: {POLL_INTERVAL_SEC}s, "
        f"disconnect debounce: {DISCONNECT_DEBOUNCE_SEC}s, "
        f"min restart interval: {MIN_RESTART_INTERVAL_SEC}s, "
        f"lightdm failure backoff: {LIGHTDM_FAILURE_BACKOFF_SEC}s"
    )

    # Tell systemd we're ready.
    sd_notifier.notify("READY=1")

    # Detect "boot recovery" mode. jam-display-wait-for-hdmi writes the
    # flag below when it gives up after 5 minutes of polling without
    # seeing any HDMI connector. While in this mode, the normal
    # transition-detection path is insufficient because the kernel may
    # have registered the GPU without usable connectors -- no future
    # connector "appears" for us to react to. We periodically force a
    # lightdm restart instead, which causes DRM re-enumeration and
    # picks up the TV once it comes on.
    recovery_mode = HDMI_MISSING_AT_BOOT_FLAG.exists()
    if recovery_mode:
        logger.warning(
            f"Detected {HDMI_MISSING_AT_BOOT_FLAG} -- entering recovery "
            f"mode. Will force a lightdm restart every "
            f"{RECOVERY_RESTART_INTERVAL_SEC:.0f}s until HDMI is "
            f"detected."
        )

    # Initial snapshot. Whatever state HDMI is in *right now* is treated
    # as baseline -- we don't fire a restart for the existing state, only
    # for transitions away from it.
    prev_states = _hdmi_connector_states()
    logger.info(f"Initial HDMI connector states: {prev_states}")

    # If we booted with HDMI present, recovery mode is unnecessary even
    # if the flag is somehow stale -- clear it so subsequent service
    # restarts don't re-enter recovery for no reason.
    if recovery_mode and any(prev_states.values()):
        logger.info(
            "HDMI already connected at startup despite recovery flag -- "
            "skipping recovery mode and clearing the flag."
        )
        _clear_recovery_flag()
        recovery_mode = False

    # Per-connector "when did this transition to disconnected?" tracking
    # for debouncing. None means the connector is currently connected.
    # We only fire a lightdm restart on reconnect if the connector spent
    # at least DISCONNECT_DEBOUNCE_SEC in the disconnected state -- this
    # filters out sub-second EDID renegotiations that don't warrant a
    # display-stack restart and would otherwise probabilistically
    # trigger the lightdm/Wayfire crash bug. See module-level docstring
    # on DISCONNECT_DEBOUNCE_SEC.
    disconnected_at: dict[str, float] = {}

    # If we're entering recovery mode, anchor last_restart_at to NOW so
    # the first forced lightdm restart fires exactly
    # RECOVERY_RESTART_INTERVAL_SEC seconds later -- not immediately.
    # In normal mode we start at 0.0 so a legitimate reconnect within
    # the first MIN_RESTART_INTERVAL_SEC of the monitor's life isn't
    # blocked.
    last_restart_at = time.monotonic() if recovery_mode else 0.0

    # When the most recent _restart_lightdm() returned False. 0.0 means
    # no recent failure. While we're inside LIGHTDM_FAILURE_BACKOFF_SEC
    # of this timestamp, we skip ALL restart attempts (debounced
    # reconnects AND recovery-mode forced restarts). lightdm is wedged
    # and further restarts will likely just retrigger the same crash.
    last_restart_failed_at = 0.0

    # Rate-limit the recovery-mode "skipping restart" log lines so we
    # don't spam the journal once per second while in failure backoff.
    # The recovery-mode loop runs every poll iteration once the recovery
    # interval has elapsed; without this, a 5-min backoff produces ~300
    # identical WARNING lines. Still want SOME visibility while gated --
    # just not hundreds per minute.
    SKIP_LOG_INTERVAL_SEC = 60.0
    last_recovery_skip_log_at = 0.0

    last_watchdog_at = time.monotonic()

    def in_lightdm_backoff(now: float) -> tuple[bool, float]:
        """Return (in_backoff, seconds_remaining)."""
        if last_restart_failed_at == 0.0:
            return False, 0.0
        remaining = LIGHTDM_FAILURE_BACKOFF_SEC - (now - last_restart_failed_at)
        return remaining > 0, max(0.0, remaining)

    while True:
        try:
            time.sleep(POLL_INTERVAL_SEC)

            now = time.monotonic()
            if now - last_watchdog_at >= WATCHDOG_PING_INTERVAL_SEC:
                sd_notifier.notify("WATCHDOG=1")
                last_watchdog_at = now

            current_states = _hdmi_connector_states()

            # Track disconnects: stamp the moment a connector transitions
            # to disconnected, so we can measure how long it stayed
            # disconnected when it later reconnects.
            for name, prev_connected in prev_states.items():
                now_connected = current_states.get(name, False)
                if prev_connected and not now_connected:
                    # Just transitioned to disconnected -- start the
                    # debounce timer.
                    disconnected_at[name] = now
                    logger.info(
                        f"HDMI disconnect detected on: {name} "
                        f"(starting {DISCONNECT_DEBOUNCE_SEC:.0f}s debounce)"
                    )

            # Detect any disconnected -> connected transition AND check
            # the debounce.
            for name, now_connected in current_states.items():
                prev_connected = prev_states.get(name, False)
                if not (now_connected and not prev_connected):
                    continue  # Not a reconnect transition.

                # This connector just reconnected. How long was it down?
                down_since = disconnected_at.pop(name, None)
                if down_since is None:
                    # We never observed the disconnect (probably because
                    # this is the connector's FIRST appearance, e.g. it
                    # came online after boot). Treat as real -- it's a
                    # legitimate "HDMI came up" event we want to react to.
                    logger.info(
                        f"HDMI reconnect detected on: {name} "
                        f"(no prior disconnect timestamp -- treating as real)"
                    )
                    down_duration = float("inf")
                else:
                    down_duration = now - down_since
                    logger.info(
                        f"HDMI reconnect detected on: {name} "
                        f"(was disconnected for {down_duration:.1f}s)"
                    )

                # Debounce: skip if the disconnect was too brief.
                if down_duration < DISCONNECT_DEBOUNCE_SEC:
                    logger.info(
                        f"Ignoring brief disconnect/reconnect on {name} "
                        f"({down_duration:.1f}s < {DISCONNECT_DEBOUNCE_SEC:.0f}s "
                        f"debounce). No lightdm restart needed -- the TV's "
                        f"existing session is still valid."
                    )
                    continue

                # Failure backoff check: don't slam a wedged lightdm.
                in_backoff, backoff_remaining = in_lightdm_backoff(now)
                if in_backoff:
                    logger.warning(
                        f"Skipping lightdm restart for {name} reconnect: "
                        f"in failure backoff ({backoff_remaining:.0f}s "
                        f"remaining). Last lightdm restart attempt failed; "
                        f"holding off to avoid re-triggering the crash."
                    )
                    continue

                # Rate-limit restarts.
                since_last = now - last_restart_at
                if since_last < MIN_RESTART_INTERVAL_SEC:
                    remaining = MIN_RESTART_INTERVAL_SEC - since_last
                    logger.warning(
                        f"Skipping lightdm restart (rate limit: "
                        f"{remaining:.1f}s remaining). Customer would see "
                        f"display blackout otherwise; will catch the next "
                        f"reconnect after the interval."
                    )
                    continue

                # All gates passed: actually restart lightdm.
                if _restart_lightdm():
                    last_restart_at = now
                else:
                    last_restart_failed_at = now
                    logger.error(
                        f"lightdm restart failed -- entering "
                        f"{LIGHTDM_FAILURE_BACKOFF_SEC:.0f}s backoff. Will "
                        f"not attempt further restarts during that window."
                    )

            # Recovery-mode tick: if we came up headless and STILL see no
            # HDMI after RECOVERY_RESTART_INTERVAL_SEC, force a lightdm
            # restart. The same backoff + rate-limit gates apply -- a
            # wedged lightdm doesn't get better by pounding on it.
            # Once any HDMI shows True we exit recovery mode entirely.
            if recovery_mode:
                if any(current_states.values()):
                    logger.info(
                        f"HDMI now detected ({current_states}) -- exiting "
                        f"recovery mode."
                    )
                    _clear_recovery_flag()
                    recovery_mode = False
                else:
                    since_last = now - last_restart_at
                    if since_last >= RECOVERY_RESTART_INTERVAL_SEC:
                        in_backoff, backoff_remaining = in_lightdm_backoff(now)
                        if in_backoff:
                            # Rate-limited: this branch fires every poll
                            # iteration (every 1s) while we're past the
                            # recovery interval AND in backoff. Logging
                            # at full rate would emit ~300 identical
                            # lines per backoff window.
                            if now - last_recovery_skip_log_at >= SKIP_LOG_INTERVAL_SEC:
                                logger.warning(
                                    f"Recovery mode: would force lightdm restart "
                                    f"but in failure backoff "
                                    f"({backoff_remaining:.0f}s remaining). "
                                    f"Holding off. (Suppressing further "
                                    f"messages for {SKIP_LOG_INTERVAL_SEC:.0f}s.)"
                                )
                                last_recovery_skip_log_at = now
                        else:
                            logger.warning(
                                f"Recovery mode: still no HDMI after "
                                f"{since_last:.0f}s. Forcing lightdm restart "
                                f"to trigger DRM re-enumeration."
                            )
                            if _restart_lightdm():
                                last_restart_at = now
                            else:
                                last_restart_failed_at = now
                                logger.error(
                                    f"Recovery-mode lightdm restart failed -- "
                                    f"entering {LIGHTDM_FAILURE_BACKOFF_SEC:.0f}s "
                                    f"backoff."
                                )

            prev_states = current_states

        except Exception as e:
            # Don't let a transient sysfs hiccup crash the daemon. Log
            # and keep going.
            logger.error(f"Poll loop error (continuing): {e}", exc_info=True)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
