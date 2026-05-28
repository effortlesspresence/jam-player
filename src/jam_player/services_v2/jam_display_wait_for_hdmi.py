#!/usr/bin/env python3
"""
jam-display-wait-for-hdmi.service

Blocks lightdm from starting until at least one DRM connector reports
status=connected with a real EDID negotiated. This prevents the "4 duplicate
quadrants on a 4K TV" artifact that occurs when Wayfire/Xwayland comes up
against the kernel's default 1920x1080 virtual fallback, then later
mis-handles the hotplug-driven resize to the TV's native 4K mode.

Customer-visible failure mode being prevented:
  1. Power plugged into Pi
  2. Pi boots, no HDMI attached yet -> KMS falls back to virtual 1920x1080
  3. lightdm + Wayfire + jam-player-display all start at 1920x1080
  4. Customer plugs in 4K TV
  5. Pi's framebuffer is now 1920x1080 stretched/tiled to fill 3840x2160
  6. Screen shows JAM Player UI as 4 duplicated quadrants

By blocking lightdm until HDMI is actually connected, Wayfire starts up
already knowing the TV's native resolution (4K or otherwise) and renders
everything correctly from the first frame.

Polls /sys/class/drm/card*-HDMI-A-*/status for "connected".

Behavior:
  - If HDMI is already connected at boot (the common case), returns
    immediately. lightdm starts ~instantly with the correct resolution.
  - If HDMI is not connected, polls every POLL_INTERVAL_SEC seconds for up
    to MAX_WAIT_SEC before giving up and allowing lightdm to start anyway
    (so the JP isn't bricked in display terms if a customer never attaches
    a TV -- the remote-management services still need to come up).
  - Logs the negotiated mode for observability.

Exit code is always 0 -- success on detect, success on timeout (timeout is
not a JAM Player failure, just a customer config issue we can't act on
from here).
"""
import sys
import time
from pathlib import Path

from common.logging_config import setup_service_logging

logger = setup_service_logging("jam-display-wait-for-hdmi")


# How often to poll DRM connector status.
POLL_INTERVAL_SEC = 0.5

# How long to wait before giving up and letting lightdm start anyway.
# 5 minutes -- well past any reasonable boot delay, short enough that a JP
# without a TV attached still finishes booting in a bounded time so remote
# management services can run.
MAX_WAIT_SEC = 300

# Which DRM connector glob patterns count as "a real display".
# On Pi 4/5 with KMS, HDMI shows up as HDMI-A-1 (and HDMI-A-2 on Pi 5 dual
# HDMI). We treat any connected HDMI-A-* port as success.
HDMI_GLOB = "card*-HDMI-A-*"

# Runtime flag file written when wait-for-hdmi gives up without seeing
# any HDMI connector. Read by jam-display-hotplug-monitor at startup --
# its presence indicates the system booted "headless" (no display
# present at boot), which is a known failure mode where the kernel may
# register the GPU as card0 with no real connectors and subsequent
# hotplug events never fire as the connectors don't physically exist
# from the kernel's point of view. The hotplug-monitor uses this flag
# to enter a recovery mode that periodically forces a lightdm restart
# (which re-enumerates DRM) until a real display is finally detected.
#
# Lives in /run/ so it's wiped on reboot -- a fresh boot with a TV
# attached should not inherit recovery mode from a previous headless
# boot. Matches the existing convention from /run/jam-update-in-progress.
HDMI_MISSING_AT_BOOT_FLAG = Path("/run/jam-hdmi-was-missing-at-boot")


def _list_hdmi_connectors() -> list[Path]:
    """Return all DRM connector directories that look like HDMI ports."""
    return list(Path("/sys/class/drm").glob(HDMI_GLOB))


def _is_connected(connector_dir: Path) -> bool:
    """True if the connector reports status=connected."""
    status_file = connector_dir / "status"
    try:
        return status_file.read_text().strip() == "connected"
    except OSError:
        return False


def _read_mode(connector_dir: Path) -> str | None:
    """Return the active mode (e.g. '3840x2160') for an HDMI connector, or None."""
    modes_file = connector_dir / "modes"
    try:
        first_mode = modes_file.read_text().strip().splitlines()
        return first_mode[0] if first_mode else None
    except OSError:
        return None


def wait_for_hdmi() -> bool:
    """
    Poll until an HDMI connector reports connected, or until MAX_WAIT_SEC.

    Returns True on detection, False on timeout. Either way the service
    exits 0 -- the boolean is for logging only.
    """
    deadline = time.monotonic() + MAX_WAIT_SEC

    # Fast path: check once before logging the "waiting" message so the
    # common case (HDMI already connected at boot) doesn't pollute logs.
    connectors = _list_hdmi_connectors()
    for c in connectors:
        if _is_connected(c):
            mode = _read_mode(c)
            logger.info(f"HDMI already connected at {c.name} (mode: {mode})")
            return True

    logger.info(
        f"No HDMI connected yet. Polling every {POLL_INTERVAL_SEC}s "
        f"(max {MAX_WAIT_SEC}s)..."
    )

    while time.monotonic() < deadline:
        for c in _list_hdmi_connectors():
            if _is_connected(c):
                mode = _read_mode(c)
                logger.info(f"HDMI connected at {c.name} (mode: {mode})")
                return True
        time.sleep(POLL_INTERVAL_SEC)

    logger.warning(
        f"Timed out after {MAX_WAIT_SEC}s waiting for HDMI. Letting lightdm "
        f"start anyway -- display won't render content until HDMI is plugged in "
        f"and the hotplug monitor restarts lightdm."
    )
    # Mark the system as "booted headless" so jam-display-hotplug-monitor
    # enters its recovery mode. Without this flag the monitor would just
    # set its baseline to {all False} and wait for a False->True
    # transition that may never come if the kernel registered the GPU
    # without a usable connector (the "card0 with no crtc or sizes"
    # state seen on Pi 5 reboots where the TV was off). Best-effort write
    # -- if /run is unwritable we log and continue, since the monitor
    # gracefully falls back to its normal transition-detection path when
    # the flag is absent.
    try:
        HDMI_MISSING_AT_BOOT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        HDMI_MISSING_AT_BOOT_FLAG.touch()
        logger.info(
            f"Created {HDMI_MISSING_AT_BOOT_FLAG} -- "
            f"hotplug-monitor will enter recovery mode."
        )
    except OSError as e:
        logger.error(
            f"Could not write {HDMI_MISSING_AT_BOOT_FLAG}: {e}. "
            f"Hotplug recovery mode will NOT activate on this boot."
        )
    return False


def main() -> int:
    logger.info("=" * 60)
    logger.info("JAM Display - Wait for HDMI Service Starting")
    logger.info("=" * 60)
    wait_for_hdmi()
    return 0


if __name__ == "__main__":
    sys.exit(main())
