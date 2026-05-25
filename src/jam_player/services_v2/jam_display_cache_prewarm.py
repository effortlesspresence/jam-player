#!/usr/bin/env python3
"""
Standalone script that pre-renders every cacheable display screen at
common resolutions (1920x1080, 3840x2160) and writes the PNGs to
/var/cache/jam-player-display/.

Why this exists as a separate entry point (rather than just being
called inline from jam-first-boot or jam-update):

  - jam-first-boot needs to trigger pre-warm on FRESH images that
    won't see a "real" jam-update run during initial setup (the device
    boots into the latest commit, jam-update exits early as "already
    up to date", never runs the install pipeline, never calls
    prewarm_display_cache). Without pre-warm in this path, every
    setup-flow screen (AWAITING_REGISTRATION, AWAITING_SCREEN_LINK,
    NO_ACTIVE_SCENES, DOWNLOADING_CONTENT) renders fresh on first
    encounter -- ~10-15s of CPU per screen at 4K, all customer-visible.

  - We don't want jam-first-boot to BLOCK on pre-warm. First-boot
    must complete quickly so jam-ble-provisioning + jam-player-display
    can start. Spawning this script as a detached background process
    (Popen + start_new_session=True from jam-first-boot) lets pre-warm
    run in parallel with everything else coming up.

  - We don't want jam-update to BLOCK on pre-warm either. jam-update's
    existing prewarm step happens inline; this script provides an
    alternative async invocation path for jam-first-boot specifically.

Idempotent + race-safe with jam-player-display's lazy rendering:
display_cache.get_or_render_cached() writes via .tmp.png + atomic
rename, and the read path requires the final file to exist with non-
zero size. Worst case during a concurrent prewarm: jam-player-display
renders the same screen itself, both processes atomically rename to
the same target, last writer wins, identical content -- no corruption,
no partial-image flash.

Exits 0 on success or partial-success (some screens rendered, some
failed). Logs failures but doesn't propagate them. Pre-warm is best-
effort; a failure here just means slower lazy renders on first use --
not a customer-blocker.
"""

import os
import sys

# Add the services directory to path for common module imports.
# (Same pattern used by other services_v2 scripts.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.credentials import get_device_uuid
from common.display_cache import prewarm_display_cache
from common.logging_config import setup_service_logging
from jam_player_display import build_display_render_registry

logger = setup_service_logging("jam-display-cache-prewarm")


def main() -> int:
    logger.info("=" * 60)
    logger.info("JAM Display Cache Pre-Warm Starting")
    logger.info("=" * 60)

    try:
        registry = build_display_render_registry()
    except Exception as e:
        logger.exception(f"Could not build display render registry: {e}")
        return 1

    device_uuid = get_device_uuid()
    if not device_uuid:
        # ABORT. Every cached screen embeds the device UUID in its
        # footer (see the `if device_uuid:` blocks in each
        # create_*_screen function). If we let pre-warm run with a
        # None UUID, we'd write PNGs with NO footer to the cache --
        # and the display service would later serve those UUID-less
        # PNGs from disk indefinitely without ever re-rendering them
        # with the correct UUID. Better to do nothing here and let
        # the display service's lazy-render path produce correct
        # PNGs on first use (slower customer experience for one
        # transition, but no permanently-broken cache).
        #
        # In the happy path this branch never fires: jam-first-boot
        # creates device_uuid.txt very early, and only spawns this
        # pre-warm script AFTER mark_complete() runs at the end. If
        # we're seeing this log message, something is wrong upstream.
        logger.error(
            "No device UUID found -- aborting pre-warm. The display "
            "service will lazy-render screens on first encounter. "
            "Investigate why device_uuid.txt is missing."
        )
        return 1

    try:
        summary = prewarm_display_cache(registry, device_uuid=device_uuid)
        logger.info(
            f"Pre-warm complete: rendered={summary['rendered']}, "
            f"skipped={summary['skipped']}, failed={summary['failed']}"
        )
        return 0
    except Exception as e:
        logger.exception(f"prewarm_display_cache raised: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
