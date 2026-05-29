#!/usr/bin/env python3
"""
jam-display-hotplug-monitor.service

Observability daemon that watches HDMI connector state and logs
disconnect/reconnect events. Does NOT take any action on hotplug events.

Why this service exists:
  - Wayfire's wlroots DRM backend handles HDMI hotplug natively
    (output add/remove, mode change, EDID re-read) via kernel uevents.
    We don't need to do anything to make hotplug work for the customer.
  - But the kernel's own log output for HDMI hotplug events is hard to
    correlate to wall-clock customer experience. When a customer
    reports "screen went black at 3:14 PM", we want one human-readable
    log line per HDMI state transition so we can build a timeline.
  - So this service polls /sys/class/drm and writes a clean log entry
    per real disconnect/reconnect. That's its only job.

Polls /sys/class/drm/card*-HDMI-A-*/status once per second. Lightweight
(just a couple of stat+read syscalls per second).

History (grep-archaeology):
  Earlier versions of this service restarted lightdm.service on HDMI
  reconnect, on the assumption that Wayfire wouldn't pick up the new
  EDID otherwise. That was wrong on two counts:

    1. Wayfire DOES pick up HDMI hotplug events automatically via
       wlroots' DRM backend. The lightdm restart was never needed.

    2. The lightdm restart actively caused customer-visible black
       screens. It briefly spawned a new Xorg, which grabbed the DRM
       master from mpv. Then lightdm hit a latent GLib NULL-pointer
       crash in its autologin->greeter handoff (a Bookworm-specific
       bug) and died. The crash released the DRM master, but in the
       window before that, mpv was unable to present frames. On
       static-image scenes (no incoming IPC loadfile to force a render
       retry), mpv never reclaimed the surface and the screen stayed
       black until manual reboot.

  The lightdm restart was removed. Wayfire handles hotplug correctly
  on its own; we just observe and log.
"""
import sys
import time
from pathlib import Path

import sdnotify

from common.logging_config import setup_service_logging

logger = setup_service_logging("jam-display-hotplug-monitor")
sd_notifier = sdnotify.SystemdNotifier()


# Poll interval. 1s for prompt observability with negligible CPU.
POLL_INTERVAL_SEC = 1.0

# How long a disconnect must persist before we log the eventual
# reconnect as a "real" disconnect/reconnect cycle vs a brief CEC/EDID
# flicker. Pure log-noise filter -- this service no longer takes any
# action on hotplug events, but TVs flap their DRM connector several
# times per day from CEC handshakes / power-save / mode renegotiation,
# and logging every sub-second flip floods the journal and makes real
# customer events hard to find.
DISCONNECT_DEBOUNCE_SEC = 15.0

# systemd watchdog ping interval. Service unit sets WatchdogSec=30 so we
# ping every 10s for headroom.
WATCHDOG_PING_INTERVAL_SEC = 10.0

# DRM connector glob.
HDMI_GLOB = "card*-HDMI-A-*"


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


def main() -> int:
    logger.info("=" * 60)
    logger.info("JAM Display Hotplug Monitor Starting")
    logger.info("=" * 60)
    logger.info(
        f"Poll interval: {POLL_INTERVAL_SEC}s, "
        f"disconnect debounce: {DISCONNECT_DEBOUNCE_SEC}s. "
        f"Observability only -- Wayfire handles HDMI hotplug via DRM "
        f"uevents on its own. This service takes no action."
    )

    # Tell systemd we're ready.
    sd_notifier.notify("READY=1")

    # Initial snapshot. Whatever state HDMI is in *right now* is treated
    # as baseline -- transitions away from this state get logged.
    prev_states = _hdmi_connector_states()
    logger.info(f"Initial HDMI connector states: {prev_states}")

    # Per-connector "when did this transition to disconnected?" tracking
    # so we can measure how long it stayed down when it later reconnects
    # (used for the log-debounce only).
    disconnected_at: dict[str, float] = {}

    last_watchdog_at = time.monotonic()

    while True:
        try:
            time.sleep(POLL_INTERVAL_SEC)

            now = time.monotonic()
            if now - last_watchdog_at >= WATCHDOG_PING_INTERVAL_SEC:
                sd_notifier.notify("WATCHDOG=1")
                last_watchdog_at = now

            current_states = _hdmi_connector_states()

            # Track disconnects: stamp the moment a connector transitions
            # to disconnected.
            for name, prev_connected in prev_states.items():
                now_connected = current_states.get(name, False)
                if prev_connected and not now_connected:
                    disconnected_at[name] = now
                    logger.info(f"HDMI disconnect detected on: {name}")

            # Detect disconnected -> connected transitions.
            for name, now_connected in current_states.items():
                prev_connected = prev_states.get(name, False)
                if not (now_connected and not prev_connected):
                    continue  # Not a reconnect transition.

                down_since = disconnected_at.pop(name, None)
                if down_since is None:
                    # First appearance of this connector (came online
                    # after boot, no prior disconnect timestamp).
                    logger.info(
                        f"HDMI connect detected on: {name} "
                        f"(no prior disconnect timestamp)"
                    )
                    continue

                down_duration = now - down_since

                if down_duration < DISCONNECT_DEBOUNCE_SEC:
                    # Brief flicker (CEC, power-save, etc.) -- skip the
                    # log line to keep the journal readable.
                    continue

                logger.info(
                    f"HDMI reconnect detected on: {name} "
                    f"(was disconnected for {down_duration:.1f}s)"
                )

            prev_states = current_states

        except Exception as e:
            # Don't let a transient sysfs hiccup crash the daemon. Log
            # and keep going.
            logger.error(f"Poll loop error (continuing): {e}", exc_info=True)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
