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
    Restart lightdm.service via systemctl. Returns True on success.

    Intentionally synchronous -- we want to know whether the restart
    completed before resuming polling, since racing another restart
    would be bad.
    """
    logger.info("Restarting lightdm.service to pick up new HDMI EDID...")
    try:
        result = subprocess.run(
            ["systemctl", "restart", "lightdm.service"],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("lightdm.service restarted successfully")
            return True
        logger.error(
            f"lightdm restart failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("lightdm restart timed out after 60s")
        return False
    except Exception as e:
        logger.error(f"lightdm restart raised: {e}")
        return False


def main() -> int:
    logger.info("=" * 60)
    logger.info("JAM Display Hotplug Monitor Starting")
    logger.info("=" * 60)
    logger.info(
        f"Poll interval: {POLL_INTERVAL_SEC}s, "
        f"min restart interval: {MIN_RESTART_INTERVAL_SEC}s"
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

    # If we're entering recovery mode, anchor last_restart_at to NOW so
    # the first forced lightdm restart fires exactly
    # RECOVERY_RESTART_INTERVAL_SEC seconds later -- not immediately.
    # In normal mode we start at 0.0 so a legitimate reconnect within
    # the first MIN_RESTART_INTERVAL_SEC of the monitor's life isn't
    # blocked.
    last_restart_at = time.monotonic() if recovery_mode else 0.0
    last_watchdog_at = time.monotonic()

    while True:
        try:
            time.sleep(POLL_INTERVAL_SEC)

            now = time.monotonic()
            if now - last_watchdog_at >= WATCHDOG_PING_INTERVAL_SEC:
                sd_notifier.notify("WATCHDOG=1")
                last_watchdog_at = now

            current_states = _hdmi_connector_states()

            # Detect any disconnected -> connected transition.
            reconnected = [
                name
                for name, connected in current_states.items()
                if connected and not prev_states.get(name, False)
            ]

            if reconnected:
                logger.info(
                    f"HDMI reconnect detected on: {', '.join(reconnected)}"
                )
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
                else:
                    if _restart_lightdm():
                        last_restart_at = now

            # Track disconnects too, purely for logs / observability.
            disconnected = [
                name
                for name, was_connected in prev_states.items()
                if was_connected and not current_states.get(name, False)
            ]
            if disconnected:
                logger.info(
                    f"HDMI disconnect detected on: {', '.join(disconnected)} "
                    f"(no action -- waiting for reconnect)"
                )

            # Recovery-mode tick: if we came up headless and STILL see no
            # HDMI after RECOVERY_RESTART_INTERVAL_SEC, force a lightdm
            # restart. Don't bother re-checking 'reconnected' above --
            # if a real reconnect just fired we already restarted and
            # last_restart_at was updated, so the rate-limit check here
            # will skip and prev_states will record the True states for
            # next iteration. Once any HDMI shows True we exit recovery
            # mode entirely.
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
                        logger.warning(
                            f"Recovery mode: still no HDMI after "
                            f"{since_last:.0f}s. Forcing lightdm restart "
                            f"to trigger DRM re-enumeration."
                        )
                        if _restart_lightdm():
                            last_restart_at = now

            prev_states = current_states

        except Exception as e:
            # Don't let a transient sysfs hiccup crash the daemon. Log
            # and keep going.
            logger.error(f"Poll loop error (continuing): {e}", exc_info=True)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
