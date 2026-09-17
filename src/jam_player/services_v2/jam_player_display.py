#!/usr/bin/env python3
"""
JAM Player Display Service - Unified Display Manager

This service handles all display states for the JAM Player with modern,
premium gradient-based UI design.

Display Modes (in order of setup progress):

1. AWAITING_NETWORK (.internet_verified flag does not exist)
   - Device has no verified internet connection. Covers "no WiFi configured",
     "WiFi configured but not connected", and "WiFi connected but no real
     internet reachable" -- all three cases require the same user action
     (re-run WiFi setup via the mobile app over BLE).
   - Display: setup screen with JAM logo, QR code, "Set up your JAM Player"

2. AWAITING_REGISTRATION (internet verified, but .registered flag is missing)
   - Device is online but has not yet been registered to a Location via
     the mobile app. Registration is a mobile-app-only flow (the web app
     cannot register devices because registration depends on the BLE
     session the mobile app already has open). Internally, the device
     may or may not be .announced at this point -- announce is plumbing
     and doesn't gate anything the user sees.
   - Display: "Almost there. Set up this JAM Player in the JAM Player
     Setup app on your phone." + mobile-app QR.

3. AWAITING_SCREEN_LINK (registered, but no screen_id on disk)
   - Device is registered to a Location but the user hasn't linked it
     to a specific Screen yet. Linking can happen from the mobile app
     OR the web app.
   - Display: "Connected! Link this JAM Player to a screen using the
     JAM Player Setup app or the web app."

4. DOWNLOADING_CONTENT (screen_id exists, scenes.json missing OR has scenes
   with media files not yet on disk)
   - Content fetch/download is in progress.
   - Display: "Waiting for content..." with animated dots

5. NO_ACTIVE_SCENES (screen_id exists, scenes.json exists but is an empty list)
   - The linked Screen has no active scenes configured right now. This is
     distinct from DOWNLOADING_CONTENT: there is nothing to download, the
     backend deliberately returned no scenes.
   - Display: "This screen has no active scenes..."

6. PLAYING_CONTENT (scenes.json has scenes and at least one media file exists)
   - Display: Plays scenes sequentially from scenes.json
   - Wall clock synchronized playback for multi-display setups
   - Automatically reloads when content is updated

7. OUTLET_INACTIVE (cached outlet operational status is not OPERATIONAL)
   - The Location this JAM Player is registered to has been deactivated,
     is off-season, is scheduled for a future window, or its temporary
     period has ended. The device suppresses content playback and shows
     a "this outlet is inactive" message directing the customer back to
     the web app to reactivate.
   - This check overrides PLAYING_CONTENT: we explicitly do NOT keep
     showing paid content for a non-operational outlet, even if the
     device is offline. The flip side: if the device has no cached
     outlet status (older fielded build, never online), we fail open
     and behave as if the outlet is OPERATIONAL.

IMPORTANT: PLAYING_CONTENT is evaluated FIRST in determine_display_mode(),
before any network/setup-state checks. This preserves offline playback:
a previously-configured device that loses internet (restaurant WiFi drops,
deployment in a venue with no WiFi, etc.) keeps playing its cached content
instead of reverting to a setup screen. The single exception is
OUTLET_INACTIVE, which is evaluated even earlier -- see
determine_display_mode() for the full spec.

This service monitors state changes and transitions between display modes automatically.
"""

import sys
import os
import time
import json
import socket
import subprocess
import signal
from pathlib import Path
from enum import Enum
from typing import Optional, Any, List, Dict
from datetime import datetime, time as dt_time

# Add the services directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.network import is_internet_verified, get_mac_addresses
from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    device_identity_lines,
    mac_identity_lines,
    is_device_registered,
    get_device_uuid,
    get_screen_id,
    get_display_orientation,
)
from common.system import get_systemd_notifier, setup_signal_handlers, check_chrony_sync
from common.paths import (
    INTERNET_VERIFIED_FLAG,
    BOOT_IDENTITY_SHOWN_FLAG, PLYMOUTH_SPLASH, PLYMOUTH_SPLASH_BACKUP,
)
from common.display_cache import (
    cleanup_stale_cache,
    get_or_render_cached,
    DISPLAY_CACHE_DIR,
)
from common.outlet_status import (
    is_outlet_operational,
    read_outlet_name,
    read_outlet_status,
)
from jam_player import constants

# Try to import PIL for setup screens
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError as e:
    HAS_PIL = False
    print(f"WARNING: PIL not available: {e}")

# Try to import qrcode for setup screens
try:
    import qrcode
    HAS_QRCODE = True
except ImportError as e:
    HAS_QRCODE = False
    print(f"WARNING: qrcode not available: {e}")

logger = setup_service_logging('jam-player-display')

# Log dependency status at startup
def _log_dependency_status():
    """Log the availability of optional dependencies."""
    logger.info(f"Dependency status: PIL={HAS_PIL}, qrcode={HAS_QRCODE}")

    # Check for feh
    try:
        result = subprocess.run(['which', 'feh'], capture_output=True, text=True)
        has_feh = result.returncode == 0
        logger.info(f"feh available: {has_feh} ({result.stdout.strip() if has_feh else 'not found'})")
    except Exception as e:
        logger.warning(f"Could not check for feh: {e}")

    # Check for ImageMagick (fallback)
    try:
        result = subprocess.run(['which', 'convert'], capture_output=True, text=True)
        has_imagemagick = result.returncode == 0
        logger.info(f"ImageMagick available: {has_imagemagick} ({result.stdout.strip() if has_imagemagick else 'not found'})")
    except Exception as e:
        logger.warning(f"Could not check for ImageMagick: {e}")
sd_notifier = get_systemd_notifier()


def get_rotation_angle() -> int:
    """
    Get the MPV rotation angle based on display orientation setting.

    Returns:
        Rotation angle in degrees (0, 90, or 270)
    """
    orientation = get_display_orientation()

    # Map orientation to MPV rotation angle
    orientation_to_rotation = {
        'LANDSCAPE': 0,
        'PORTRAIT_BOTTOM_ON_LEFT': 270,
        'PORTRAIT_BOTTOM_ON_RIGHT': 90,
    }

    rotation = orientation_to_rotation.get(orientation, 0)
    logger.debug(f"Display orientation: {orientation} -> rotation: {rotation}")
    return rotation


# =============================================================================
# Sync Configuration - for multi-display wall clock synchronization
# =============================================================================

# How often to check sync (ms)
SYNC_CHECK_INTERVAL_MS = 200

# Only seek if drift exceeds this (emergency correction)
SEEK_THRESHOLD_MS = 500

# Consider "in sync" if within this tolerance
TARGET_SYNC_TOLERANCE_MS = 10

# Proportional speed control - adjust playback speed based on drift magnitude
# Offset ranges and corresponding speed adjustments:
#   0-10ms:    normal speed (1.0x)
#   10-30ms:   gentle correction (1.01x / 0.99x)
#   30-100ms:  moderate correction (1.03x / 0.97x)
#   100-500ms: aggressive correction (1.05x / 0.95x)
SPEED_NORMAL = 1.0
SPEED_GENTLE_FAST = 1.01
SPEED_GENTLE_SLOW = 0.99
SPEED_MODERATE_FAST = 1.03
SPEED_MODERATE_SLOW = 0.97
SPEED_AGGRESSIVE_FAST = 1.05
SPEED_AGGRESSIVE_SLOW = 0.95

# --- Wall-clock seek-on-load sync (multi-screen video walls only) ------------
# Kill-switch: if this file exists, wall sync is force-disabled fleet-wide
# (fast off-ramp without a redeploy).
WALL_SYNC_KILL_SWITCH = Path('/etc/jam/disable_wall_sync')
# Don't seek for offsets below this -- imperceptible on a wall, and a seek
# would cause more visible disruption than it fixes.
WALL_SYNC_MIN_SEEK_MS = 250
# How often to re-check chrony sync (it shells out to chronyc; don't do it on
# every scene load). A late-converging clock starts being trusted within this.
WALL_SYNC_CLOCK_RECHECK_S = 30

# --- mpv restart policy (2026-09) -------------------------------------------
# When mpv cannot be started -- nothing playable on disk yet, no display
# session, no output attached -- the service must stay ALIVE and keep trying.
# Those conditions clear by themselves (content finishes downloading, the TV is
# plugged back in), and the unit has WatchdogSec=60 with StartLimitBurst=5/300,
# so any path that waits WITHOUT pinging the watchdog gets the service killed
# and, after five kills, left `failed` with a dark screen until a human
# intervenes. Retries back off between these bounds and every wait pings the
# watchdog. See docs/BENCH_TESTS.md group J.
MPV_RESTART_BACKOFF_MIN_SEC = 2
# Capped near STATE_CHECK_INTERVAL_SEC: a longer wait inside the playback loop
# also delays the main loop's next mode re-evaluation, so an outlet that is
# deactivated, a screen that is unlinked, or content that is deleted would take
# that much longer to leave the screen.
MPV_RESTART_BACKOFF_MAX_SEC = 10
# Longest a single sleep slice may run before re-pinging the watchdog.
WATCHDOG_SLEEP_SLICE_SEC = 5
# If mpv reports a successful start and then dies again straight away, we are
# in a start-die loop. Restarting it as fast as the CPU allows just churns
# processes and log writes, so a burst is slowed by this many seconds at most.
# The FIRST exit is never delayed: one-off crash recovery stays as immediate as
# it has always been.
MPV_CRASH_BURST_MAX_WAIT_SEC = 5

# Outcomes of _start_video_playback. Deliberately strings, not a bool: the
# caller MUST tell "everything is scheduled off right now" apart from "I could
# not start mpv". They are not interchangeable -- the first is a normal daily
# state that owns its own branded screen, the second is a fault. Compare
# against these constants explicitly; every one of them is truthy, so
# `if _start_video_playback():` is always wrong.
PLAYBACK_STARTED = 'started'
PLAYBACK_NOTHING_SCHEDULED = 'nothing_scheduled'
PLAYBACK_NO_PLAYABLE_FILE = 'no_playable_file'
PLAYBACK_MPV_FAILED = 'mpv_failed'
# Throttle for the "display is stuck" ERROR line. ERROR-level records are
# shipped to the backend's Logs & Errors panel by the logging pipeline, which
# is how this reaches the fleet view.
DISPLAY_TROUBLE_LOG_INTERVAL_SEC = 600


class DisplayMode(Enum):
    """
    Display modes for JAM Player.

    Values listed below in logical setup-progress order (which also
    matches the order the ladder in determine_display_mode() falls
    through). Note that determine_display_mode() checks PLAYING_CONTENT
    FIRST (before the ladder), so an offline device with cached content
    keeps playing -- see that function's docstring for the full spec.
    """
    AWAITING_NETWORK = "awaiting_network"  # .internet_verified flag missing
    AWAITING_REGISTRATION = "awaiting_registration"  # online, but not yet registered to a Location (.registered missing)
    AWAITING_SCREEN_LINK = "awaiting_screen_link"  # registered, but no screen_id
    DOWNLOADING_CONTENT = "downloading_content"  # screen linked, content not yet on disk
    NO_ACTIVE_SCENES = "no_active_scenes"  # screen linked, backend returned empty scenes list
    PLAYING_CONTENT = "playing_content"  # scenes + media present
    OUTLET_INACTIVE = "outlet_inactive"  # cached outlet operational status is not OPERATIONAL


# =============================================================================
# Scheduling Helpers - Filter scenes by day/time
# =============================================================================

# Map Python weekday (0=Monday) to API day names
WEEKDAY_NAMES = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']


def parse_time_str(time_str: str) -> Optional[dt_time]:
    """Parse 'HH:MM' string to datetime.time object."""
    if not time_str:
        return None
    try:
        parts = time_str.split(':')
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def is_scene_scheduled_now(scene: Dict[str, Any]) -> bool:
    """
    Check if a scene should be displayed right now based on its daysScheduled.

    Rules from the design doc:
    1. If current day is NOT in daysScheduled list → don't display
    2. If current day IS in list but startTime/endTime are null → display all day
    3. If current day IS in list with startTime + endTime → only display during that range

    Args:
        scene: Scene dict with optional 'days_scheduled' field

    Returns:
        True if scene should be displayed now, False otherwise
    """
    days_scheduled = scene.get('days_scheduled', [])

    # If no scheduling info, always display (backwards compatibility)
    if not days_scheduled:
        return True

    now = datetime.now()
    current_weekday = WEEKDAY_NAMES[now.weekday()]
    current_time = now.time()

    # Find schedule entry for current day
    for schedule in days_scheduled:
        day_of_week = schedule.get('dayOfWeek')
        # Handle both formats: string "FRIDAY" or object {"value": "FRIDAY", "label": "Friday"}
        if isinstance(day_of_week, dict):
            day_of_week = day_of_week.get('value')
        if day_of_week != current_weekday:
            continue

        # Found entry for today
        start_time_str = schedule.get('startTime')
        end_time_str = schedule.get('endTime')

        # If no time constraints, display all day
        if not start_time_str and not end_time_str:
            return True

        # If we have time constraints, check them
        start_time = parse_time_str(start_time_str)
        end_time = parse_time_str(end_time_str)

        if start_time and end_time:
            # Handle overnight schedules (e.g., 22:00 to 02:00)
            if start_time <= end_time:
                # Normal range (e.g., 09:00 to 17:00)
                if start_time <= current_time <= end_time:
                    return True
            else:
                # Overnight range (e.g., 22:00 to 02:00)
                if current_time >= start_time or current_time <= end_time:
                    return True
        elif start_time:
            # Only start time - display from start time until midnight
            if current_time >= start_time:
                return True
        elif end_time:
            # Only end time - display from midnight until end time
            if current_time <= end_time:
                return True

        # Time constraints not met
        return False

    # Current day not in schedule list - don't display
    return False


def filter_scenes_by_schedule(scenes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filter scenes to only those that should be displayed right now.

    Args:
        scenes: List of scene dicts

    Returns:
        Filtered list of scenes that are scheduled for now
    """
    filtered = [s for s in scenes if is_scene_scheduled_now(s)]
    if len(filtered) != len(scenes):
        logger.info(f"Schedule filter: {len(filtered)}/{len(scenes)} scenes active now")
    return filtered


# =============================================================================
# Configuration Constants
# =============================================================================

# JAM Brand Colors (from web app design system)
JAM_ORANGE_PRIMARY = (255, 107, 53)    # #FF6B35 - Vibrant Orange
JAM_ORANGE_SECONDARY = (247, 147, 30)  # #F7931E - Golden Orange
JAM_RED = (196, 30, 58)                # #C41E3A - Deep Red
JAM_GOLD = (212, 175, 55)              # #D4AF37 - Gold
JAM_DARK = (31, 41, 55)                # #1F2937 - Dark Gray
JAM_DARKER = (17, 24, 39)              # #111827 - Darker background

# Display configuration
BACKGROUND_COLOR = JAM_DARKER
TEXT_COLOR = (255, 255, 255)  # White
ACCENT_COLOR = JAM_ORANGE_PRIMARY
SECONDARY_COLOR = (156, 163, 175)  # #9CA3AF - Muted gray

# Base font sizes - calibrated for a 1080p (1080-pixel-tall) reference
# display. On larger displays (e.g. 4K = 2160px), call _scaled(base, height)
# to linearly scale up so text and QR codes stay legible at viewing distance.
# Do NOT use these raw constants directly inside render functions -- always
# wrap them in _scaled() so 4K renders correctly. Keeping them as int
# constants (vs scaled defaults) makes the calibration values easy to find
# and tune.
FONT_SIZE_TITLE = 72
FONT_SIZE_SUBTITLE = 36
FONT_SIZE_INSTRUCTIONS = 32
FONT_SIZE_URL = 28
FONT_SIZE_DEVICE_ID = 24
FONT_SIZE_TAGLINE = 42

# Identity block geometry (1080p reference values; everything goes through
# _scaled). The block is drawn bottom-up from IDENTITY_BOTTOM_OFFSET above the
# bottom edge: the muted lines IDENTITY_MUTED_LEADING line-heights apart, the
# two prominent lines IDENTITY_BIG_LEADING apart. It grew by two MAC lines in
# 2026-09 and collided with the setup screen's tagline, because the screens laid
# themselves out top-down with fixed gaps and assumed the block's height. They
# now lay out against _identity_block_top() and keep IDENTITY_CLEARANCE above
# it; the two QR screens shrink their code to fit, never below QR_MIN_SIZE.
IDENTITY_BOTTOM_OFFSET = 42
IDENTITY_MUTED_LEADING = 1.35
IDENTITY_BIG_LEADING = 1.5
IDENTITY_BIG_SEPARATION = 0.2
IDENTITY_CLEARANCE = 20
QR_MIN_SIZE = 160

# Reference display height that the FONT_SIZE_* constants are calibrated to.
# All scaling is linear vs this baseline -- a 4K screen (2160px) renders
# everything at 2x, a 720p screen (720px) renders at 0.67x, etc.
REFERENCE_SCREEN_HEIGHT = 1080

# Logo path on device. The logo PNG is installed to the comitup user's home
# during JP setup; if missing (e.g. shipped image lacks it), render functions
# fall back to skipping the logo entirely and shift other elements up.
JAM_LOGO_PATH = "/home/comitup/jam_player_logo.png"

# URLs for setup
UNIVERSAL_SETUP_URL = "https://setup.justamenu.com"

# State checking intervals
STATE_CHECK_INTERVAL_SEC = 5

# Cache key for the "No Content Scheduled" screen rendered by
# create_no_scheduled_content_screen(). This is NOT a DisplayMode value
# (it's a sub-state of PLAYING_CONTENT shown when all scenes are
# scheduled off right now), but it shares the same cache infrastructure
# as the proper DisplayMode-keyed screens so jam-update can pre-warm it
# alongside the others. Without caching this screen rendered inline at
# ~10-15s per 4K transition -- see the 2026-05-24 incident.
NO_SCHEDULED_CONTENT_CACHE_KEY = "no_scheduled_content"

# The device-identity screen shown briefly at every boot, and installed as the
# Plymouth boot splash once a player has real data to show. Not a DisplayMode:
# it is a one-shot screen the service puts up before its first real mode, so it
# is registered in the render map purely so jam-update pre-warms it.
BOOT_IDENTITY_CACHE_KEY = "boot_identity"
# How long it stays up. Well under the unit's WatchdogSec=60, and the wait pings
# the watchdog anyway. Every boot pays this before content appears, including
# the nightly 3 AM reboot.
BOOT_IDENTITY_HOLD_SECONDS = 15

# Coordination flag for jam-update.service. While this file exists, the
# display loop pauses its "feh died -> respawn it" behavior so that
# jam-update can show the update-in-progress screen without us racing it
# back to the foreground. jam-update creates the file at start of
# show_updating_screen() and removes it in hide_updating_screen().
# Must stay in sync with the UPDATE_IN_PROGRESS_FLAG constant in
# jam_update.py -- single hardcoded path so we don't need a shared
# module that both services import (jam_update.py keeps its imports
# minimal to survive its self-re-execution flow).
UPDATE_IN_PROGRESS_FLAG = Path('/run/jam-update-in-progress')

# Maximum time the display loop will defer to jam-update before treating
# the coordination flag as stale. A legitimate install takes ~2-5
# minutes; 5 minutes gives generous headroom while ensuring a crashed
# jam-update doesn't leave the customer's display in standby
# indefinitely. Tuned conservatively -- if we ever have an install that
# legitimately takes longer than this, the customer briefly sees the
# old display through the tail of the install, which is a much better
# failure mode than a permanently-frozen display.
UPDATE_FLAG_MAX_AGE_SEC = 5 * 60


def _is_update_flag_stale() -> bool:
    """
    Return True if the update-in-progress flag has been present longer
    than UPDATE_FLAG_MAX_AGE_SEC. Defensive against jam-update crashing
    or being SIGKILLed between writing the flag and removing it.

    Returns False (not stale) if the flag doesn't exist or if we can't
    read its mtime -- err on the side of trusting an active flag,
    since we have the parallel _jam_update_service_is_active() check
    as the second gate.
    """
    try:
        mtime = UPDATE_IN_PROGRESS_FLAG.stat().st_mtime
        age = time.time() - mtime
        return age > UPDATE_FLAG_MAX_AGE_SEC
    except OSError:
        return False


def _jam_update_service_is_active() -> bool:
    """
    Return True if jam-update.service is currently active per systemd.

    Used as a sanity check on the update-in-progress flag: if the flag
    exists but the service isn't running, the flag is stale (jam-update
    crashed without cleanup) and we should ignore it.

    Returns False on any error (subprocess timeout, systemctl missing,
    etc.) -- safer to assume the service is NOT running and let the
    display recover, than to assume it IS running and stay paused.
    """
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'jam-update.service'],
            capture_output=True,
            text=True,
            timeout=3,
        )
        # systemctl prints "active" / "inactive" / "failed" / etc.
        # Only "active" or "activating" means jam-update is genuinely
        # running. "inactive" means it finished or never started.
        return result.stdout.strip() in ('active', 'activating')
    except Exception:
        return False


def _scaled(base_size: int, screen_height: int) -> int:
    """
    Scale a size value linearly relative to a 1080p reference.

    Used so font sizes, logo heights, QR sizes etc. grow proportionally
    with screen resolution. A 4K display (2160px tall) gets 2x sizes,
    1440p gets 1.33x, 720p gets 0.67x. Result is always >= 1 so PIL
    doesn't choke on a zero-sized font.

    Args:
        base_size: Size in pixels calibrated for a 1080p display
        screen_height: Actual display height in pixels

    Returns:
        Scaled size in pixels, minimum 1
    """
    return max(1, int(base_size * screen_height / REFERENCE_SCREEN_HEIGHT))


# =============================================================================
# Display Image Generation (for non-playing modes)
# =============================================================================

def get_fb_size() -> tuple:
    """Get framebuffer dimensions."""
    try:
        with open('/sys/class/graphics/fb0/virtual_size', 'r') as f:
            w, h = f.read().strip().split(',')
            return int(w), int(h)
    except:
        return 1920, 1080


def get_font(size: int, bold: bool = True):
    """Get a font, falling back to default if needed."""
    if not HAS_PIL:
        return None

    if bold:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    else:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    # No TrueType face on this system. Pillow >= 10.1 can still size its
    # built-in face; older Pillow only has the fixed ~10 px bitmap font.
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# The device's permanent MACs are an immutable hardware fact, read at most
# once per process and reused by every setup-screen render. A module-level memo
# is the right scope here (unlike a per-call flag): it is shared across the
# many render functions that draw the identity block and lives for the process.
# Only a SUCCESSFUL read (at least one MAC) is cached, so a rare read before
# NetworkManager is ready is retried on the next render rather than stuck.
_cached_display_macs = None  # type: Optional[Dict[str, Optional[str]]]


def _get_display_macs() -> Dict[str, Optional[str]]:
    """Memoized permanent WiFi/ethernet MACs for the identity block."""
    global _cached_display_macs
    if _cached_display_macs is not None:
        return _cached_display_macs
    macs = get_mac_addresses()
    if macs.get('wifiMac') or macs.get('ethernetMac'):
        _cached_display_macs = macs  # cache only a successful read
    return macs


def _identity_block_geometry(height: int, n_muted: int) -> dict:
    """
    Pixel geometry of the identity block for a display `height` px tall with
    `n_muted` small lines (the UUID plus 0-2 MAC lines).

    Shared by _draw_device_identity (which draws it) and _identity_block_top
    (which the screens lay out against), so the two can never disagree.
    Returns the "mm"-anchored y of each line, top to bottom, and `top`: the
    pixel row the block's highest glyphs reach.
    """
    big_px = _scaled(FONT_SIZE_URL, height)
    small_px = _scaled(FONT_SIZE_DEVICE_ID, height)
    y = height - _scaled(IDENTITY_BOTTOM_OFFSET, height)
    muted_ys = []
    for _ in range(n_muted):
        muted_ys.append(y)
        y -= int(small_px * IDENTITY_MUTED_LEADING)
    y -= int(big_px * IDENTITY_BIG_SEPARATION)
    setup_y = y
    y -= int(big_px * IDENTITY_BIG_LEADING)
    id_y = y
    # "mm"-anchored text reaches about 0.6 of the font size above its centre.
    return {
        'muted_ys': list(reversed(muted_ys)),
        'setup_y': setup_y,
        'id_y': id_y,
        'top': id_y - int(big_px * 0.6),
    }


def _identity_block_top(height: int, device_uuid) -> int:
    """
    The pixel row a screen's own content must stay above: the top of the
    identity block _draw_device_identity will draw for this device, or the
    bottom margin when there is no UUID and nothing will be drawn.
    """
    if not device_identity_lines(device_uuid):
        return height - _scaled(IDENTITY_BOTTOM_OFFSET, height)
    macs = _get_display_macs()
    n_muted = 1 + len(mac_identity_lines(macs.get('wifiMac'), macs.get('ethernetMac')))
    return _identity_block_geometry(height, n_muted)['top']


def _fit_qr_size(nominal: int, available: int, height: int, what: str) -> int:
    """
    The QR edge that fits in `available` vertical pixels: `nominal` when there
    is room (every normal display), smaller when the identity block leaves
    less, never below QR_MIN_SIZE -- a code that cannot be scanned is worse
    than a tight layout on a display too short for this design to begin with.
    """
    size = max(_scaled(QR_MIN_SIZE, height), min(nominal, available))
    if size < nominal:
        logger.info(f"QR code reduced to {size}px (nominal {nominal}px) to clear the identity block on the {what}")
    return size


def _draw_device_identity(draw, width: int, height: int, device_uuid) -> bool:
    """
    The identity block at the bottom of every non-content screen.

    Prominent (big), then muted support lines (small/secondary), top to bottom:

        Device ID: XXXXX                      <- what a person actually matches
        Setup network: JAM-PLAYER-XXXXX       <- the phone's Bluetooth list entry
        Device: <full uuid>                   <- for support, deliberately muted
        Wi-Fi MAC: AA:BB:CC:DD:EE:FF          <- support; omitted if unknown
        Ethernet MAC: AA:BB:CC:DD:EE:11       <- support; omitted if none

    Users could not match the last five characters of a printed UUID to the
    JAM-PLAYER-XXXXX name in their phone's Bluetooth list, so the screen says
    both outright. The five characters come from the same derivation the BLE
    service advertises, so the two can never disagree. The MAC lines let support
    tie a screen to the addresses the backend/dashboards report. Sits above the
    "v2" marker at height - 25; draws nothing when there is no UUID yet.
    Screens must keep their own content above _identity_block_top().

    Returns whether this render is safe to CACHE. It is not when the device has
    a UUID but the MACs could not be read at all (NetworkManager not ready yet):
    persisting a MAC-less PNG to /var/cache would show it until the next commit.
    Callers fold the result into their cacheable flag, mirroring the qrcode
    fallback. A device that legitimately has no ethernet still returns True --
    its single Wi-Fi line is the permanent truth and safe to cache.
    """
    lines = device_identity_lines(device_uuid)
    if not lines:
        return True  # nothing to identify yet; nothing MAC-dependent to gate
    macs = _get_display_macs()
    mac_lines = mac_identity_lines(macs.get('wifiMac'), macs.get('ethernetMac'))
    cacheable = bool(macs.get('wifiMac') or macs.get('ethernetMac'))

    center_x = width // 2
    big = get_font(_scaled(FONT_SIZE_URL, height), bold=True)
    small = get_font(_scaled(FONT_SIZE_DEVICE_ID, height), bold=False)

    # Muted support block (small/secondary): the full UUID plus any MAC lines,
    # with the two prominent lines above it. Every position comes from
    # _identity_block_geometry, the same numbers the screens lay out against
    # via _identity_block_top(), so the block can never grow into content.
    muted = [lines[2]] + mac_lines
    geo = _identity_block_geometry(height, len(muted))
    for text, y in zip(muted, geo['muted_ys']):
        draw.text((center_x, y), text, font=small, fill=SECONDARY_COLOR, anchor="mm")
    draw.text((center_x, geo['setup_y']), lines[1], font=big, fill=TEXT_COLOR, anchor="mm")
    draw.text((center_x, geo['id_y']), lines[0], font=big, fill=TEXT_COLOR, anchor="mm")
    return cacheable


def create_mesh_gradient_background(width: int, height: int, theme: str = "vibrant") -> Image.Image:
    """
    Create a vibrant mesh gradient background with multiple color points.

    This creates the colorful gradient effect seen in modern app designs,
    with colors blending smoothly across the image.

    Args:
        width: Image width
        height: Image height
        theme: Color theme - "vibrant" (setup), "cool" (loading), "warm" (off-hours)

    Returns:
        PIL Image with mesh gradient background
    """
    import math

    img = Image.new('RGB', (width, height))

    # Define color anchor points for each theme
    # Each point is (x_ratio, y_ratio, (r, g, b))
    themes = {
        "vibrant": [
            # Deep blue/purple top
            (0.5, 0.0, (65, 40, 180)),
            # Pink/magenta left side
            (0.0, 0.4, (180, 50, 140)),
            # Orange/yellow center-left glow
            (0.2, 0.5, (255, 140, 50)),
            # Cyan/blue bottom-right
            (1.0, 0.8, (40, 160, 220)),
            # Purple bottom-left
            (0.0, 1.0, (120, 60, 180)),
            # Blue bottom
            (0.5, 1.0, (60, 100, 200)),
        ],
        "cool": [
            # Deep blue top
            (0.5, 0.0, (30, 60, 150)),
            # Teal left
            (0.0, 0.5, (40, 140, 160)),
            # Purple right
            (1.0, 0.3, (100, 60, 160)),
            # Cyan bottom
            (0.5, 1.0, (50, 180, 200)),
            # Blue bottom-left
            (0.0, 1.0, (40, 80, 180)),
        ],
        "warm": [
            # Purple top
            (0.5, 0.0, (100, 50, 150)),
            # Orange left
            (0.0, 0.5, (220, 100, 50)),
            # Pink right
            (1.0, 0.4, (200, 80, 140)),
            # Magenta bottom
            (0.5, 1.0, (160, 60, 130)),
            # Deep red bottom-left
            (0.0, 1.0, (150, 40, 80)),
        ],
    }

    color_points = themes.get(theme, themes["vibrant"])

    # Process in chunks for speed (every 2 pixels, then interpolate)
    step = 2
    pixels = []

    for y in range(0, height, step):
        row = []
        for x in range(0, width, step):
            # Normalize coordinates
            nx = x / width
            ny = y / height

            # Calculate weighted color based on distance to each anchor point
            total_weight = 0.0
            r_sum, g_sum, b_sum = 0.0, 0.0, 0.0

            for px, py, color in color_points:
                # Distance from this pixel to the color point
                dx = nx - px
                dy = ny - py
                dist = math.sqrt(dx * dx + dy * dy)

                # Inverse distance weighting with falloff
                # Add small epsilon to avoid division by zero
                weight = 1.0 / (dist * dist * 4 + 0.01)

                r_sum += color[0] * weight
                g_sum += color[1] * weight
                b_sum += color[2] * weight
                total_weight += weight

            # Normalize
            r = int(min(255, max(0, r_sum / total_weight)))
            g = int(min(255, max(0, g_sum / total_weight)))
            b = int(min(255, max(0, b_sum / total_weight)))

            row.append((r, g, b))

        pixels.append(row)

    # Draw the gradient
    draw = ImageDraw.Draw(img)
    for yi, row in enumerate(pixels):
        y = yi * step
        for xi, color in enumerate(row):
            x = xi * step
            # Draw a small rectangle for each sampled point
            draw.rectangle([x, y, x + step, y + step], fill=color)

    return img


def load_and_scale_logo(target_height: int) -> Optional[Image.Image]:
    """
    Load the JAM logo and scale it to the target height while maintaining aspect ratio.

    Args:
        target_height: Desired height in pixels

    Returns:
        PIL Image of scaled logo, or None if logo not found
    """
    if not HAS_PIL:
        return None

    if not os.path.exists(JAM_LOGO_PATH):
        logger.warning(f"Logo not found at {JAM_LOGO_PATH}")
        return None

    try:
        logo = Image.open(JAM_LOGO_PATH)

        # Convert to RGBA if needed for transparency support
        if logo.mode != 'RGBA':
            logo = logo.convert('RGBA')

        # Calculate new dimensions maintaining aspect ratio
        aspect = logo.width / logo.height
        new_width = int(target_height * aspect)

        logo = logo.resize((new_width, target_height), Image.Resampling.LANCZOS)
        return logo
    except Exception as e:
        logger.error(f"Failed to load logo: {e}")
        return None


def generate_qr_code(url: str, size: int = 300) -> Optional[Image.Image]:
    """Generate a QR code image for the given URL."""
    if not HAS_PIL:
        return None

    if not HAS_QRCODE:
        # Return a placeholder if qrcode module not available
        img = Image.new('RGB', (size, size), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, size-1, size-1], outline=(0, 0, 0), width=2)
        draw.text((size//4, size//2), "QR Code", fill=(0, 0, 0))
        return img

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)

    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_img = qr_img.resize((size, size), Image.Resampling.NEAREST)

    return qr_img


def create_unregistered_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the setup screen for AWAITING_NETWORK mode.

    Modern gradient design with:
    - JAM Player logo
    - "JAM Player" title
    - Setup instructions
    - QR code
    - "Get ready to JAM." tagline
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    # Create vibrant mesh gradient background
    img = create_mesh_gradient_background(width, height, theme="vibrant")
    draw = ImageDraw.Draw(img)

    # Fonts (scaled to display resolution)
    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height))
    instructions_font = get_font(_scaled(FONT_SIZE_INSTRUCTIONS, height), bold=False)
    tagline_font = get_font(_scaled(FONT_SIZE_TAGLINE, height))

    center_x = width // 2

    # Layout dimensions scale with display height. 1080p reference values:
    # logo=120, qr=320. On 4K these become 240/640. The gaps scale too --
    # fixed-pixel gaps left this screen short of room on 720p and floating
    # on 4K. Everything the screen draws itself ends above content_bottom;
    # the identity block (_draw_device_identity) owns the rest.
    logo_height = _scaled(120, height)
    qr_nominal = _scaled(320, height)
    border_padding = _scaled(8, height)
    content_bottom = _identity_block_top(height, device_uuid) - _scaled(IDENTITY_CLEARANCE, height)

    # Start from top with some padding
    y = _scaled(52, height)

    # Logo
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        # Paste with alpha mask for transparency
        img.paste(logo, (logo_x, y), logo if logo.mode == 'RGBA' else None)
        y += logo.height + _scaled(24, height)
    else:
        # Fallback: draw a simple placeholder or skip
        y += _scaled(40, height)

    # "JAM Player" title with gradient-like orange
    title = "JAM Player"
    draw.text(
        (center_x, y),
        title,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), title, font=title_font)
    y += bbox[3] + _scaled(30, height)

    # Instruction text
    instruction = "Set up your JAM Player with the JAM Player Setup App."
    draw.text(
        (center_x, y),
        instruction,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), instruction, font=instructions_font)
    y += bbox[3] + _scaled(14, height)

    # "Scan the QR code to begin."
    scan_text = "Scan the QR code to begin."
    draw.text(
        (center_x, y),
        scan_text,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), scan_text, font=instructions_font)
    y += bbox[3] + _scaled(30, height)

    # QR code, sized to the room left above the tagline and the identity
    # block -- full size on every normal display (see _fit_qr_size).
    tagline = "Get ready to JAM."
    tagline_height = draw.textbbox((0, 0), tagline, font=tagline_font)[3]
    tagline_gap = _scaled(40, height)
    qr_size = _fit_qr_size(
        qr_nominal,
        content_bottom - y - border_padding - tagline_gap - tagline_height,
        height,
        f"setup screen at {width}x{height}",
    )
    qr_img = generate_qr_code(UNIVERSAL_SETUP_URL, qr_size)
    if qr_img:
        qr_x = center_x - qr_size // 2
        qr_y = y

        # Draw subtle orange border around QR code
        draw.rectangle(
            [qr_x - border_padding, qr_y - border_padding,
             qr_x + qr_size + border_padding, qr_y + qr_size + border_padding],
            outline=JAM_ORANGE_PRIMARY,
            width=max(2, _scaled(3, height))
        )

        img.paste(qr_img, (qr_x, qr_y))
        y += qr_size + border_padding + tagline_gap

    # "Get ready to JAM." tagline
    draw.text(
        (center_x, y),
        tagline,
        font=tagline_font,
        fill=JAM_ORANGE_SECONDARY,
        anchor="mt"
    )

    # Device UUID at bottom (small, subtle)
    identity_cacheable = _draw_device_identity(draw, width, height, device_uuid)

    # Version indicator in bottom-right corner
    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),  # Very subtle
        anchor="mm"
    )

    # If `qrcode` wasn't importable when this module loaded (e.g. fresh
    # 1.0->2.0 migration where jam-player-display started before
    # jam-update's pip-install finished), generate_qr_code() falls back
    # to drawing a "QR Code" text placeholder. Returning cacheable=False
    # tells display_cache to write this render to /tmp only -- NOT to
    # /var/cache -- so the next call (after jam-update restarts this
    # service with deps installed) re-renders with a real QR. Without
    # this, the placeholder PNG would be cached under the current commit
    # hash and served forever until the commit changed.
    return img, (HAS_QRCODE and identity_cacheable)


def create_waiting_for_content_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the screen for DOWNLOADING_CONTENT mode.

    Modern gradient design showing content download progress message.

    Footer shows the device UUID (not the screen ID) because any screen
    that isn't real content should identify the physical JAM Player for
    support purposes. The screen ID is only meaningful inside the web
    app; the device UUID is what uniquely identifies the hardware a
    technician is looking at.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    # Create cool mesh gradient background for loading state
    img = create_mesh_gradient_background(width, height, theme="cool")
    draw = ImageDraw.Draw(img)

    # Fonts (scaled to display resolution)
    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height), bold=False)

    center_x = width // 2
    center_y = height // 2

    # Logo at top (scaled). 1080p reference: 80px; 4K: 160px.
    logo_height = _scaled(80, height)
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        logo_y = int(height * 0.15)
        img.paste(logo, (logo_x, logo_y), logo if logo.mode == 'RGBA' else None)

    # Title - centered
    title = "Waiting for content..."
    draw.text(
        (center_x, center_y - _scaled(40, height)),
        title,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mm"
    )

    # Subtitle
    subtitle = "Content is being downloaded. This may take a few minutes."
    draw.text(
        (center_x, center_y + _scaled(70, height)),
        subtitle,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mm"
    )

    # Animated-looking dots (static, but gives impression of activity)
    # Draw three dots with varying opacity to suggest animation
    # Scaled like the text above: fixed offsets put the 4K title and subtitle
    # on top of each other (caught by tests/test_display_screen_layout.py).
    dot_y = center_y + _scaled(150, height)
    dot_spacing = _scaled(30, height)
    dot_radius = _scaled(8, height)
    for i, alpha in enumerate([255, 180, 100]):
        dot_x = center_x + (i - 1) * dot_spacing
        dot_color = (
            int(JAM_ORANGE_SECONDARY[0] * alpha / 255),
            int(JAM_ORANGE_SECONDARY[1] * alpha / 255),
            int(JAM_ORANGE_SECONDARY[2] * alpha / 255)
        )
        draw.ellipse(
            [dot_x - dot_radius, dot_y - dot_radius,
             dot_x + dot_radius, dot_y + dot_radius],
            fill=dot_color
        )

    # Device UUID at bottom (every non-content screen shows device UUID
    # so support can identify the physical JAM Player regardless of
    # setup state).
    identity_cacheable = _draw_device_identity(draw, width, height, device_uuid)

    # Version indicator
    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm"
    )

    return img, identity_cacheable


def create_awaiting_screen_link_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the screen for AWAITING_SCREEN_LINK mode.

    Shown when the device has verified internet connectivity but has not
    been linked to a Screen yet. Communicates that setup is partially
    complete and directs the user to the mobile or web app to finish.

    Visually distinct from the AWAITING_NETWORK setup screen: a cool-
    themed gradient (matches DOWNLOADING_CONTENT's family) emphasizes
    "you're past the WiFi step"; no primary QR code (the user already
    has the app open), just device UUID for reference.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = create_mesh_gradient_background(width, height, theme="cool")
    draw = ImageDraw.Draw(img)

    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height), bold=False)
    instructions_font = get_font(_scaled(FONT_SIZE_INSTRUCTIONS, height), bold=False)

    center_x = width // 2

    # Logo (scaled). 1080p reference: 120px; 4K: 240px.
    logo_height = _scaled(120, height)
    y = int(height * 0.12)
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        img.paste(logo, (logo_x, y), logo if logo.mode == 'RGBA' else None)
        y += logo.height + 40
    else:
        y += 40

    # Primary heading: "Registered!" -- the user just completed the
    # registration step in the mobile app, so this is the appropriate
    # celebratory checkpoint. "Connected!" was too ambiguous given
    # AWAITING_REGISTRATION also mentions the device being connected
    # to the internet.
    heading = "Registered!"
    draw.text(
        (center_x, y),
        heading,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), heading, font=title_font)
    y += bbox[3] + 20

    # Secondary line: "Almost there."
    sub = "Your JAM Player is connected to the internet."
    draw.text(
        (center_x, y),
        sub,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), sub, font=subtitle_font)
    y += bbox[3] + 20

    # Confirmation of the registration state: by the time we reach
    # AWAITING_SCREEN_LINK we know the .registered flag exists, so this
    # statement is always accurate here. Helps users understand that
    # the "link to a screen" step is the only remaining action.
    status_line = "Your JAM Player is registered to your outlet."
    draw.text(
        (center_x, y),
        status_line,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), status_line, font=subtitle_font)
    y += bbox[3] + 40

    # Instruction: link this JAM Player
    line1 = "Link this JAM Player to a screen"
    draw.text(
        (center_x, y),
        line1,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), line1, font=instructions_font)
    y += bbox[3] + 12

    line2 = "using the JAM Player Setup app or the web app."
    draw.text(
        (center_x, y),
        line2,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )

    # Device UUID at the bottom so support / users can identify this JP
    # in the app / web UI when linking.
    identity_cacheable = _draw_device_identity(draw, width, height, device_uuid)

    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm"
    )

    return img, identity_cacheable


def create_awaiting_registration_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the screen for AWAITING_REGISTRATION mode.

    Shown when the device has verified internet connectivity but has not
    been registered to a Location yet. Registration is mobile-app-only
    (the web app cannot register devices because registration depends
    on the BLE session the mobile app has open), so this screen directs
    the user specifically to the mobile app and includes a QR code to
    the app download / setup landing page.

    Distinct from AWAITING_SCREEN_LINK, which is shown after the device
    is registered but not yet linked to a specific Screen; that screen
    directs the user to either the mobile app or the web app and has no
    QR code.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = create_mesh_gradient_background(width, height, theme="cool")
    draw = ImageDraw.Draw(img)

    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height), bold=False)
    instructions_font = get_font(_scaled(FONT_SIZE_INSTRUCTIONS, height), bold=False)

    center_x = width // 2

    # Layout proportions mirror create_unregistered_screen so the user
    # doesn't experience a jarring layout shift when transitioning from
    # AWAITING_NETWORK to AWAITING_REGISTRATION. Scaled to display height,
    # gaps included, and laid out above the identity block like that screen.
    logo_height = _scaled(120, height)
    qr_nominal = _scaled(320, height)
    border_padding = _scaled(8, height)
    content_bottom = _identity_block_top(height, device_uuid) - _scaled(IDENTITY_CLEARANCE, height)

    y = _scaled(52, height)

    # Logo
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        img.paste(logo, (logo_x, y), logo if logo.mode == 'RGBA' else None)
        y += logo.height + _scaled(24, height)
    else:
        y += _scaled(40, height)

    # Primary heading: "Almost there."
    heading = "Almost there."
    draw.text(
        (center_x, y),
        heading,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), heading, font=title_font)
    y += bbox[3] + _scaled(20, height)

    # Status confirmation: explicitly reassure the user that the device
    # is online. AWAITING_REGISTRATION is only reached when
    # .internet_verified exists, so this statement is always accurate
    # here. Helps users understand that the "go to the mobile app" step
    # is the one remaining action, not a connectivity problem.
    # Instruction line 1
    line1 = "Set up this JAM Player in the"
    draw.text(
        (center_x, y),
        line1,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), line1, font=instructions_font)
    y += bbox[3] + _scaled(12, height)

    line2 = "JAM Player Setup app on your phone."
    draw.text(
        (center_x, y),
        line2,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), line2, font=instructions_font)
    y += bbox[3] + _scaled(30, height)

    # "Scan the QR code to begin."
    scan_text = "Scan the QR code to continue."
    draw.text(
        (center_x, y),
        scan_text,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), scan_text, font=instructions_font)
    y += bbox[3] + _scaled(30, height)

    # QR Code pointing at the mobile-app setup landing page, sized to the
    # room left above the identity block (full size on every normal display).
    qr_size = _fit_qr_size(
        qr_nominal,
        content_bottom - y - border_padding,
        height,
        f"registration screen at {width}x{height}",
    )
    qr_img = generate_qr_code(UNIVERSAL_SETUP_URL, qr_size)
    if qr_img:
        qr_x = center_x - qr_size // 2
        qr_y = y

        draw.rectangle(
            [qr_x - border_padding, qr_y - border_padding,
             qr_x + qr_size + border_padding, qr_y + qr_size + border_padding],
            outline=JAM_ORANGE_PRIMARY,
            width=max(2, _scaled(3, height))
        )
        img.paste(qr_img, (qr_x, qr_y))

    # Device UUID at the bottom
    identity_cacheable = _draw_device_identity(draw, width, height, device_uuid)

    # Version indicator
    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm"
    )

    # See create_unregistered_screen for the rationale -- this screen
    # also embeds a QR code, and on a first-boot/migration race where
    # the `qrcode` Python package isn't yet installed, we fall back to
    # a text-only "QR Code" placeholder. Flag the render uncacheable so
    # the next call re-renders once jam-update has installed deps.
    return img, (HAS_QRCODE and identity_cacheable)


def create_no_active_scenes_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the screen for NO_ACTIVE_SCENES mode.

    Shown when the device is fully set up (online, linked to a Screen),
    but the Screen currently has no active scenes configured. This is a
    deliberate state surfaced by the backend, not a download-in-progress
    state -- the user needs to go configure scenes in the web app.

    Visually distinct from DOWNLOADING_CONTENT (no animated dots, no
    "please wait" messaging) so the user understands the device isn't
    busy -- it's waiting on them to take action.

    Footer shows the device UUID (not the screen ID) because any screen
    that isn't real content should identify the physical JAM Player for
    support purposes. The screen ID is only meaningful inside the web
    app; the device UUID is what uniquely identifies the hardware a
    technician is looking at.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = create_mesh_gradient_background(width, height, theme="vibrant")
    draw = ImageDraw.Draw(img)

    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height), bold=False)
    instructions_font = get_font(_scaled(FONT_SIZE_INSTRUCTIONS, height), bold=False)

    center_x = width // 2

    logo_height = _scaled(120, height)
    y = int(height * 0.12)
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        img.paste(logo, (logo_x, y), logo if logo.mode == 'RGBA' else None)
        y += logo.height + 40
    else:
        y += 40

    # Heading: make clear this is not a download problem
    heading = "No active scenes"
    draw.text(
        (center_x, y),
        heading,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), heading, font=title_font)
    y += bbox[3] + 30

    sub = "This screen has nothing scheduled right now."
    draw.text(
        (center_x, y),
        sub,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), sub, font=subtitle_font)
    y += bbox[3] + 50

    line1 = "Add scenes to this screen in the web app"
    draw.text(
        (center_x, y),
        line1,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), line1, font=instructions_font)
    y += bbox[3] + 12

    line2 = "to display content here."
    draw.text(
        (center_x, y),
        line2,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt"
    )

    # Device UUID at bottom (every non-content screen shows device UUID
    # so support can identify the physical JAM Player regardless of
    # setup state).
    identity_cacheable = _draw_device_identity(draw, width, height, device_uuid)

    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm"
    )

    return img, identity_cacheable


def create_no_scheduled_content_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the "No Content Scheduled" screen.

    Distinct from create_no_active_scenes_screen: this one is shown when
    scenes exist on disk but ALL of them are scheduled off for the
    current day/time (day-of-week + time-of-day filter excluded all).
    The customer-facing message reflects that distinction -- "content
    will appear during scheduled hours" tells the customer their
    content is still configured, just not active right now.

    This used to be rendered inline at runtime by
    JamPlayerDisplayManager._show_no_scheduled_content_screen(), which
    took ~10-15s at 4K because of the mesh-gradient PIL work. Extracting
    it to a top-level factory + registering it in the display cache
    registry lets jam-update pre-warm a PNG once at install time and
    serve it instantly on every subsequent transition.

    Warm gradient theme matches the "off hours" feel.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = create_mesh_gradient_background(width, height, theme="warm")
    draw = ImageDraw.Draw(img)

    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height), bold=False)

    center_x = width // 2
    center_y = height // 2

    # Logo at top (scaled to display resolution)
    logo_height = _scaled(80, height)
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        logo_y = int(height * 0.15)
        img.paste(logo, (logo_x, logo_y), logo if logo.mode == 'RGBA' else None)

    title = "No Content Scheduled"
    draw.text(
        (center_x, center_y - _scaled(65, height)),
        title,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mm",
    )

    subtitle = "Content will appear during scheduled hours."
    draw.text(
        (center_x, center_y + _scaled(60, height)),
        subtitle,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mm",
    )

    # This screen never printed the device identity; a user stuck on "no
    # scheduled content" needs to find their player like on every other screen.
    identity_cacheable = _draw_device_identity(draw, width, height, device_uuid)

    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm",
    )

    return img, identity_cacheable


def create_outlet_inactive_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the screen for OUTLET_INACTIVE mode.

    Shown when the cached outlet operational status is anything other
    than OPERATIONAL -- the customer has deactivated this outlet, it's
    off-season, it's scheduled for the future, or its temporary
    activation window has ended. The device suppresses content playback
    and directs the customer to the web app to reactivate.

    The outlet name and the human-readable status label are read from
    the on-disk cache (populated by jam-heartbeat /
    jam-ws-commands / jam-outlet-status-poller). The label comes from
    the backend's EnumWithLabel so we don't keep an enum-to-display
    mapping in sync on-device.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    # Vibrant gradient + orange heading mirrors no-active-scenes; the
    # state is conceptually similar ("set this up in the web app") and
    # we want consistent visual language across non-content screens.
    img = create_mesh_gradient_background(width, height, theme="vibrant")
    draw = ImageDraw.Draw(img)

    title_font = get_font(_scaled(FONT_SIZE_TITLE, height))
    subtitle_font = get_font(_scaled(FONT_SIZE_SUBTITLE, height), bold=False)
    instructions_font = get_font(_scaled(FONT_SIZE_INSTRUCTIONS, height), bold=False)

    center_x = width // 2

    # Logo
    logo_height = _scaled(120, height)
    y = int(height * 0.12)
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        img.paste(logo, (logo_x, y), logo if logo.mode == 'RGBA' else None)
        y += logo.height + 40
    else:
        y += 40

    # Read cached state. Either may be None on a freshly-cached device
    # (the WS command was missed and the poller hasn't fired yet) or on
    # a partially-cached device; fall back to safe defaults.
    parsed_status = read_outlet_status()
    status_label = parsed_status[1] if parsed_status else "Inactive"
    outlet_name = read_outlet_name()

    # Heading: the outlet's name when we have it, generic copy otherwise.
    heading = (
        f"{outlet_name} is inactive" if outlet_name else "Outlet inactive"
    )
    draw.text(
        (center_x, y),
        heading,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mt",
    )
    bbox = draw.textbbox((0, 0), heading, font=title_font)
    y += bbox[3] + 20

    # Sub-line: the specific reason (Seasonal (Closed), Period Ended, ...)
    # This uses the backend's display label verbatim so we don't drift.
    status_line = f"Status: {status_label}"
    draw.text(
        (center_x, y),
        status_line,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mt",
    )
    bbox = draw.textbbox((0, 0), status_line, font=subtitle_font)
    y += bbox[3] + 50

    # Customer-facing instruction: where to go to reactivate.
    line1 = "Reactivate this outlet in the web app"
    draw.text(
        (center_x, y),
        line1,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt",
    )
    bbox = draw.textbbox((0, 0), line1, font=instructions_font)
    y += bbox[3] + 12

    line2 = "to resume content playback."
    draw.text(
        (center_x, y),
        line2,
        font=instructions_font,
        fill=TEXT_COLOR,
        anchor="mt",
    )

    # Device UUID at bottom (same convention as other non-content
    # screens, lets support identify the JP in the dashboard).
    # (Rendered inline, never cached -- so the cacheable signal is irrelevant
    # here; we ignore it and return a bare Image for the direct feh caller.)
    _draw_device_identity(draw, width, height, device_uuid)

    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm",
    )

    return img


def create_fallback_image(width: int, height: int, message: str, img_name: str) -> Optional[str]:
    """
    Create a simple fallback image using ImageMagick when PIL fails.
    Returns the path to the created image, or None if ImageMagick also fails.
    """
    img_path = f'/tmp/{img_name}.png'
    try:
        # Use ImageMagick to create a simple text image
        result = subprocess.run([
            'convert',
            '-size', f'{width}x{height}',
            'xc:black',
            '-fill', 'white',
            '-gravity', 'center',
            '-pointsize', '48',
            '-annotate', '0', message,
            img_path
        ], capture_output=True, timeout=10)

        if result.returncode == 0 and os.path.exists(img_path):
            os.chmod(img_path, 0o644)
            logger.info(f"Created fallback image with ImageMagick: {img_path}")
            return img_path
        else:
            logger.warning(f"ImageMagick failed: {result.stderr.decode()[:200]}")
            return None
    except Exception as e:
        logger.warning(f"Fallback image creation failed: {e}")
        return None


# =============================================================================
# Render Registry for Display Cache
#
# The cache layer lives in common/display_cache.py (it's used by both
# this service and jam-update at install-time for pre-warming, so it has
# to be importable from both sides). To avoid circular imports, the cache
# module accepts a registry of render functions as a parameter rather
# than importing them from this module directly. This function builds
# that registry by stringifying our DisplayMode values.
#
# Note: NO_ACTIVE_SCENES is rendered inline in
# _show_no_scheduled_content_screen() (no module-level factory exists for
# it). It's omitted from the cache registry; a future refactor could
# extract it to a top-level factory and add it here.
# =============================================================================

def build_display_render_registry() -> dict:
    """
    Build the {mode_value: render_fn} registry used by display_cache.

    Returned as plain str -> callable so display_cache stays decoupled
    from DisplayMode enum / this module. Keys are the .value of each
    DisplayMode (e.g. "awaiting_registration").
    """
    # OUTLET_INACTIVE is intentionally NOT in the cache registry: its
    # rendered content depends on the cached outlet name and status
    # label, both of which can change without the code commit changing
    # (which is what the cache keys on). Serving a stale "Bob's Diner
    # is inactive" after the device gets moved to a different outlet
    # would be worse than the ~1-2s render hit. Rendered inline by
    # transition_to_mode every time it's needed.
    # NO_SCHEDULED_CONTENT_CACHE_KEY is NOT a DisplayMode value -- it's
    # a sub-state shown by run_video_loop while still in PLAYING_CONTENT
    # mode, when all scenes happen to be scheduled off right now. We
    # register it here anyway so jam-update's pre-warm step renders it
    # at install time, making the runtime transition instant.
    return {
        DisplayMode.AWAITING_NETWORK.value: create_unregistered_screen,
        DisplayMode.AWAITING_REGISTRATION.value: create_awaiting_registration_screen,
        DisplayMode.AWAITING_SCREEN_LINK.value: create_awaiting_screen_link_screen,
        DisplayMode.DOWNLOADING_CONTENT.value: create_waiting_for_content_screen,
        DisplayMode.NO_ACTIVE_SCENES.value: create_no_active_scenes_screen,
        NO_SCHEDULED_CONTENT_CACHE_KEY: create_no_scheduled_content_screen,
        BOOT_IDENTITY_CACHE_KEY: create_boot_identity_screen,
    }


def create_boot_identity_screen(width: int, height: int, device_uuid: str = None):
    """
    The device-identity screen: what this player is, in the form support and
    the person installing it actually need.

    Deliberately a FLAT background rather than create_mesh_gradient_background.
    The gradient takes ~10-15 s to render at 4K (the 2026-05-24 incident) and
    this screen sits on the boot path; a solid fill plus text renders in well
    under a second at any resolution, so a cache miss costs the customer
    nothing.

    Returns (image, cacheable). Returns (None, False) when there is no device
    UUID yet: a freshly imaged player has nothing to identify, so the caller
    skips the hold entirely and the shipped logo splash stays in place. Not
    cacheable when a UUID exists but the MACs could not be read yet, mirroring
    _draw_device_identity -- a MAC-less PNG must not be persisted to the cache,
    and must never be promoted to the boot splash.
    """
    if not HAS_PIL:
        return None, False

    lines = device_identity_lines(device_uuid)
    if not lines:
        return None, False

    macs = _get_display_macs()
    mac_lines = mac_identity_lines(macs.get('wifiMac'), macs.get('ethernetMac'))
    cacheable = bool(mac_lines)

    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)
    center_x = width // 2

    title_font = get_font(_scaled(52, height))
    id_font = get_font(_scaled(46, height))
    body_font = get_font(_scaled(26, height), bold=False)

    # lines[0] is "Device ID: XXXXX" -- the one a person matches against a
    # label or a phone's Bluetooth list, so it gets the large type.
    rows = [(lines[0], id_font, TEXT_COLOR)]
    rows += [(line, body_font, SECONDARY_COLOR) for line in lines[1:]]
    rows += [(line, body_font, SECONDARY_COLOR) for line in mac_lines]

    gap = _scaled(48, height)
    block_top = (height - gap * len(rows)) // 2
    draw.text(
        (center_x, block_top - _scaled(86, height)),
        "JAM PLAYER",
        font=title_font,
        fill=ACCENT_COLOR,
        anchor="mm",
    )
    y = block_top
    for text, font, color in rows:
        draw.text((center_x, y), text, font=font, fill=color, anchor="mm")
        y += gap

    version_font = get_font(_scaled(14, height), bold=False)
    draw.text(
        (width - 30, height - 25), "v2", font=version_font,
        fill=(80, 80, 80), anchor="mm",
    )
    return img, cacheable


def install_boot_splash(png_path: str) -> bool:
    """
    Make a rendered identity screen the Plymouth boot splash.

    Shipped images boot the JAM logo from the `pix` theme
    (/usr/share/plymouth/themes/pix/splash.png; cmdline.txt carries `splash`).
    The logo remains the shipped default and is backed up once, on the first
    install, so it can always be restored and so a player that never generates
    a screen keeps it.

    Writes ONLY when the bytes actually differ. This runs every boot and the SD
    card is the scarcest resource on the device, so the steady state is a read
    and a compare. The write is atomic (temp file then replace) so a power cut
    can never leave Plymouth pointing at a truncated PNG.

    Entirely best-effort: every failure is swallowed, because nothing about the
    next boot's splash is worth disturbing the display that is running now.

    NOTE: if a build ever bakes the Plymouth theme into an initramfs, replacing
    the file on the root filesystem simply has no effect. That is a silent
    no-op rather than a breakage, which is what makes this safe to ship.
    """
    try:
        source = Path(png_path)
        data = source.read_bytes()
        if not data:
            logger.warning("Refusing to install an empty boot splash")
            return False
        if not PLYMOUTH_SPLASH.parent.is_dir():
            logger.info("No Plymouth pix theme on this image; leaving the splash alone")
            return False
        if PLYMOUTH_SPLASH.exists():
            if PLYMOUTH_SPLASH.read_bytes() == data:
                return True  # already current -- the common path, no write
            if not PLYMOUTH_SPLASH_BACKUP.exists():
                PLYMOUTH_SPLASH_BACKUP.write_bytes(PLYMOUTH_SPLASH.read_bytes())
                logger.info(f"Kept the shipped logo splash at {PLYMOUTH_SPLASH_BACKUP}")
        tmp = PLYMOUTH_SPLASH.with_name(PLYMOUTH_SPLASH.name + '.jam-tmp')
        tmp.write_bytes(data)
        os.replace(tmp, PLYMOUTH_SPLASH)
        logger.info(f"Installed the device identity screen as the boot splash ({len(data)} bytes)")
        return True
    except Exception as e:
        logger.warning(f"Could not install the boot splash (non-fatal): {e}")
        return False


def display_image_with_feh(img: Image.Image, img_name: str = "jam_display", fallback_message: str = None) -> Optional[subprocess.Popen]:
    """Display an image fullscreen using feh. Returns the process handle."""
    img_path = f'/tmp/{img_name}.png'

    if img is None:
        if fallback_message:
            # Try to create a fallback image with ImageMagick
            logger.warning(f"PIL image is None, attempting ImageMagick fallback")
            width, height = get_fb_size()
            img_path = create_fallback_image(width, height, fallback_message, img_name)
            if not img_path:
                logger.error("Both PIL and ImageMagick fallback failed - no image to display")
                return None
        else:
            logger.error("No image to display and no fallback message provided")
            return None
    else:
        img.save(img_path, 'PNG')
        os.chmod(img_path, 0o644)

    # Wait for X display to be available
    for _ in range(30):
        result = subprocess.run(
            ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'xdpyinfo'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        logger.warning("Display not available after 30s")
        return None

    # Launch feh
    process = subprocess.Popen(
        ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'feh', '-F', '--hide-pointer', img_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return process


def display_path_with_feh(img_path: str) -> Optional[subprocess.Popen]:
    """
    Display an already-rendered PNG file fullscreen using feh.

    Used by the cache layer to skip the PIL save step entirely on cache
    hit -- we already have a PNG on disk, just point feh at it. Shares
    X-display readiness logic with display_image_with_feh.

    Args:
        img_path: Absolute path to a PNG file readable by the comitup user

    Returns:
        feh subprocess handle, or None if X server never came up.
    """
    if not img_path or not os.path.exists(img_path):
        logger.error(f"display_path_with_feh: path does not exist: {img_path}")
        return None

    # Wait for X display to be available (same logic as display_image_with_feh)
    for _ in range(30):
        result = subprocess.run(
            ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'xdpyinfo'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        logger.warning("Display not available after 30s")
        return None

    process = subprocess.Popen(
        ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'feh', '-F', '--hide-pointer', img_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    return process


def kill_feh_processes():
    """Kill any feh processes we started."""
    try:
        subprocess.run(
            ['pkill', '-f', 'feh.*jam_display'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except:
        pass


# =============================================================================
# MPV IPC Client (for video playback)
# =============================================================================

class MpvIpcClient:
    """Client for controlling MPV via JSON IPC protocol."""

    def __init__(self, socket_path: str = "/tmp/mpv-socket"):
        self.socket_path = socket_path
        self.process: Optional[subprocess.Popen] = None
        self.socket: Optional[socket.socket] = None
        self._request_id = 0

    def start_mpv(self, rotation_angle: int = 0, loop: bool = True, initial_file: str = None) -> bool:
        """Start MPV process with IPC socket enabled.

        Args:
            rotation_angle: Video rotation in degrees
            loop: If True, loop videos infinitely (legacy mode). If False, play once (scene mode).
            initial_file: File to start playing immediately. Required - idle mode doesn't work.
        """
        if not initial_file:
            logger.error("initial_file is required - MPV idle mode doesn't display properly")
            return False

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.stop_mpv()

        mpv_args = [
            'mpv',
            '--vo=gpu',
            '--fullscreen',
            # Audio: route to the system default sink (HDMI on a Pi
            # plugged into a TV) at 100% volume. Customers control
            # loudness via their TV remote -- we deliberately don't
            # cap volume so a video can be played at the full
            # dynamic range the TV's speakers support. PipeWire's
            # default sink picks HDMI automatically when nothing
            # else is plugged in; mpv `--audio-device=auto` honors
            # that default rather than hardcoding a sink name (sink
            # names differ between Pi 4 and Pi 5).
            '--audio-device=auto',
            '--volume=100',
            '--keep-open=yes',  # Don't exit when playback ends
            '--image-display-duration=inf',  # Keep images displayed until we load next file
            '--no-osc',  # Disable on-screen controller (play/pause bar)
            '--osd-level=0',  # Disable on-screen display messages
            '--cursor-autohide=always',  # Always hide cursor
            '--no-input-default-bindings',  # Disable keyboard/mouse controls
            '--no-input-cursor',  # Disable cursor input
            f'--input-ipc-server={self.socket_path}',
            f'--video-rotate={rotation_angle}',
            initial_file,
        ]

        # Add loop option only for legacy single-video mode
        if loop:
            mpv_args.insert(-1, '--loop-file=inf')

        # Build command: run as comitup user for X11 access (service runs as root)
        args = ['sudo', '-u', 'comitup', 'env', f'DISPLAY=:0'] + mpv_args

        try:
            logger.info(f"Starting MPV as comitup with file: {initial_file}, rotation: {rotation_angle}")

            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            for _ in range(50):
                if os.path.exists(self.socket_path):
                    time.sleep(0.1)
                    return True
                time.sleep(0.1)

            logger.error("MPV socket not created within timeout")
            return False

        except Exception as e:
            logger.error(f"Failed to start MPV: {e}")
            return False

    def is_running(self) -> bool:
        """Check if MPV process is still running."""
        if self.process is None:
            return False
        return self.process.poll() is None

    def stop_mpv(self):
        """Stop the MPV process and clean up."""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except:
                pass
            self.process = None

        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except:
                pass

    def _connect(self) -> bool:
        """Establish connection to MPV socket."""
        if self.socket:
            return True

        try:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.connect(self.socket_path)
            self.socket.settimeout(2.0)
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MPV socket: {e}")
            self.socket = None
            return False

    def _send_command(self, command: list, wait_response: bool = True) -> Optional[Any]:
        """Send a command to MPV via IPC."""
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        if not self._connect():
            return None

        self._request_id += 1
        request = {
            'command': command,
            'request_id': self._request_id
        }

        try:
            msg = json.dumps(request) + '\n'
            self.socket.sendall(msg.encode('utf-8'))

            if wait_response:
                response_data = b''
                while True:
                    chunk = self.socket.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    if b'\n' in response_data:
                        break

                decoded = response_data.decode('utf-8').strip()
                for line in decoded.split('\n'):
                    if line:
                        try:
                            resp = json.loads(line)
                            if resp.get('request_id') == self._request_id:
                                if resp.get('error') == 'success':
                                    return resp.get('data')
                                else:
                                    err = resp.get('error', '')
                                    if 'unavailable' not in err.lower():
                                        logger.warning(f"MPV command {command[0]} error: {err}")
                                    return None
                        except json.JSONDecodeError:
                            continue

            return None

        except Exception as e:
            logger.error(f"Failed to send command to MPV: {e}")
            try:
                self.socket.close()
            except:
                pass
            self.socket = None
            return None

    def load_file(self, filepath: str) -> bool:
        """Load a media file into MPV and start playback."""
        self._send_command(['loadfile', filepath, 'replace'])
        # Ensure playback starts (MPV may be paused in idle mode)
        time.sleep(0.1)
        self.set_property('pause', False)
        return True

    def seek(self, position_seconds: float, exact: bool = False) -> bool:
        """Seek to a position in seconds (absolute). exact=True adds mpv's
        'exact' flag for frame-accurate (hr-seek) landing, at the cost of a
        slightly slower seek -- worth it for tight wall convergence."""
        flags = 'absolute+exact' if exact else 'absolute'
        return self._send_command(['seek', str(position_seconds), flags]) is not None

    def set_property(self, name: str, value: Any) -> bool:
        """Set an MPV property value."""
        return self._send_command(['set_property', name, value]) is not None

    def get_property(self, name: str) -> Optional[Any]:
        """Get an MPV property value."""
        return self._send_command(['get_property', name])

    def get_duration(self) -> Optional[float]:
        """Get the duration of the current file in seconds."""
        return self.get_property('duration')

    def get_playback_time(self) -> Optional[float]:
        """Get the current playback position in seconds."""
        return self.get_property('playback-time')

    def set_speed(self, speed: float) -> bool:
        """Set playback speed (1.0 = normal)."""
        return self.set_property('speed', speed)


# =============================================================================
# Main Display Manager
# =============================================================================

class JamPlayerDisplayManager:
    """
    Manages the JAM Player display across all 4 modes.
    Monitors state changes and transitions between modes.
    """

    def __init__(self):
        self.running = True
        self.current_mode: Optional[DisplayMode] = None
        self.feh_process: Optional[subprocess.Popen] = None
        self.mpv: Optional[MpvIpcClient] = None
        self.is_playing: bool = False

        # MPV crash tracking. Used for reporting only -- see _note_mpv_crash
        # for why a burst of crashes must NOT restart lightdm.
        self._mpv_crash_times: list = []
        self._mpv_crash_threshold = 5  # Number of crashes
        self._mpv_crash_window_seconds = 30  # Time window to track crashes
        # Growing wait between failed attempts to start mpv, reset on success.
        self._mpv_restart_backoff_sec: float = MPV_RESTART_BACKOFF_MIN_SEC
        # Throttle timestamps PER condition: one shared stamp let a "cannot
        # start mpv" line suppress a "mpv keeps crashing" line for ten minutes,
        # so the fleet view could only ever show one of two different faults.
        self._display_trouble_log_sec: Dict[str, float] = {}

        # Wall-clock seek-on-load sync state (multi-screen video walls)
        self._num_screens: Optional[int] = None   # layout screen count (num_screens.txt)
        self._clock_synced: bool = False           # cached check_chrony_sync() result
        self._clock_checked_at: float = 0.0        # monotonic ts of last chrony check

        # Get screen dimensions
        self.screen_width, self.screen_height = get_fb_size()
        logger.info(f"Screen dimensions: {self.screen_width}x{self.screen_height}")

        # Signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """Clean up all display processes."""
        logger.info("Cleaning up display processes...")

        if self.feh_process:
            try:
                self.feh_process.terminate()
                self.feh_process.wait(timeout=5)
            except:
                pass
            self.feh_process = None

        kill_feh_processes()

        if self.mpv:
            self.mpv.stop_mpv()
            self.mpv = None

    def determine_display_mode(self) -> DisplayMode:
        """
        Determine which display mode we should be in based on current state.

        Order of checks:
          0. Outlet operational status check (overrides everything).
             If the on-disk cache has a definite non-OPERATIONAL value,
             return OUTLET_INACTIVE. We explicitly do NOT keep playing
             paid content for a deactivated outlet even when offline.
             A missing cache file (older fielded build, never online)
             fails open and skips this check.
          1. Playable content on disk -> PLAYING_CONTENT (regardless of
             network / registration / screen link state). This is what
             keeps offline / disconnected devices showing their content
             instead of setup screens when internet drops.
          2. Otherwise walk the setup ladder:
             a. No .internet_verified -> AWAITING_NETWORK
             b. No .registered        -> AWAITING_REGISTRATION
             c. No screen_id          -> AWAITING_SCREEN_LINK
             d. scenes.json is empty  -> NO_ACTIVE_SCENES
             e. Otherwise             -> DOWNLOADING_CONTENT

        See the module docstring for the full spec.
        """
        # 0. Outlet operational status. is_outlet_operational() returns
        # True when the cache is absent OR has OPERATIONAL; only a
        # definite non-OPERATIONAL value forces OUTLET_INACTIVE. Putting
        # this above the content-first check is intentional -- a
        # deactivated outlet should NOT keep playing content even if it
        # was previously playing.
        if not is_outlet_operational():
            return DisplayMode.OUTLET_INACTIVE

        # 1. Content-first: if we have something playable, play it. This
        # is what keeps offline / disconnected devices showing their
        # content instead of setup screens when internet drops.
        content_mode = self._get_content_display_mode()
        if content_mode == DisplayMode.PLAYING_CONTENT:
            return DisplayMode.PLAYING_CONTENT

        # 2a. No playable content and no verified internet -> we can't
        # make progress on setup until WiFi is (re)configured.
        if not is_internet_verified():
            return DisplayMode.AWAITING_NETWORK

        # 2b. Online but not yet registered to a Location. Registration
        # is a mobile-app-only flow (the web app can't register a
        # device). Note we deliberately do NOT consider .announced here:
        # announce is internal plumbing. An unannounced device can still
        # be registered by the mobile app in a single call (the backend
        # transitions new -> REGISTERED directly when appropriate), so
        # the correct user-facing state is still "go to the mobile app"
        # regardless of announce status.
        if not is_device_registered():
            return DisplayMode.AWAITING_REGISTRATION

        # 2c. Registered but not linked to a specific Screen. Screen
        # linking can happen from the mobile app OR the web app. This
        # also covers the recently-unlinked case (heartbeat /
        # SET_SCREEN_ID deletes screen_id.txt on unlink).
        if not get_screen_id():
            return DisplayMode.AWAITING_SCREEN_LINK

        # 2d / 2e. Screen linked but content not playable yet. Fall back
        # to whatever the content check said (NO_ACTIVE_SCENES vs
        # DOWNLOADING_CONTENT).
        return content_mode

    def _get_content_display_mode(self) -> DisplayMode:
        """
        Resolve the content-related display mode from scenes.json + media
        files on disk. Returns one of:
          - DOWNLOADING_CONTENT: scenes.json missing, or has scenes
            listed but no media files on disk yet, or unreadable.
          - NO_ACTIVE_SCENES: scenes.json exists and is an empty list
            (backend definitively told us there are no active scenes).
          - PLAYING_CONTENT: scenes.json has scenes and at least one of
            their media files is present on disk.

        This function intentionally does NOT consult network, screen
        link, or registration state -- callers layer that on top. That
        keeps offline-playback behavior correct: a device with playable
        content always reports PLAYING_CONTENT from here regardless of
        whether it's currently online.

        On any unexpected error reading scenes.json we fall back to
        DOWNLOADING_CONTENT rather than NO_ACTIVE_SCENES, because showing
        "no scenes configured" when we actually just failed to read the
        file would be misleading.
        """
        scenes_file = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "scenes.json"
        if not scenes_file.exists():
            return DisplayMode.DOWNLOADING_CONTENT

        try:
            with open(scenes_file, 'r') as f:
                scenes = json.load(f)
        except Exception as e:
            logger.warning(f"Error reading scenes.json, treating as downloading: {e}")
            return DisplayMode.DOWNLOADING_CONTENT

        if not scenes:
            # scenes.json exists and is an empty list. scenes_manager_service
            # writes this when the backend returns zero active scenes for
            # this device's Screen -- distinct from "still downloading".
            return DisplayMode.NO_ACTIVE_SCENES

        # Scenes are listed. If at least one media file is on disk, we
        # can play. Otherwise, treat as still-downloading.
        media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
        for scene in scenes:
            media_file = scene.get('media_file')
            if media_file and (media_dir / media_file).exists():
                return DisplayMode.PLAYING_CONTENT

        return DisplayMode.DOWNLOADING_CONTENT

    def _load_scenes(self, apply_schedule_filter: bool = True) -> list:
        """
        Load scenes from the scenes.json file.

        Args:
            apply_schedule_filter: If True, filter scenes by current day/time schedule.
                                   Set to False to get all scenes regardless of schedule.

        Returns:
            List of scene dicts, sorted by order, optionally filtered by schedule.
        """
        scenes_file = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "scenes.json"
        if not scenes_file.exists():
            return []

        try:
            with open(scenes_file, 'r') as f:
                scenes = json.load(f)
            # Sort by order
            scenes.sort(key=lambda s: s.get('order', 0))

            # Filter by schedule if requested
            if apply_schedule_filter:
                scenes = filter_scenes_by_schedule(scenes)

            return scenes
        except Exception as e:
            logger.error(f"Error loading scenes: {e}")
            return []

    def _show_cached_screen(
        self,
        mode: DisplayMode,
        device_uuid: Optional[str],
        fallback_msg: str,
    ) -> Optional[subprocess.Popen]:
        """
        Render-or-fetch a setup-state screen via the display cache and
        hand the resulting PNG to feh.

        On cache hit, this is essentially instant -- feh just gets a
        path. On cache miss, we render via PIL (1-15s depending on
        resolution) and write to the cache atomically for next time.

        Falls back to display_image_with_feh's ImageMagick fallback path
        if the registry has no entry for `mode` (shouldn't happen for
        the modes we cache, but defensive).
        """
        registry = build_display_render_registry()
        render_fn = registry.get(mode.value)
        if render_fn is None:
            logger.warning(
                f"No cache registry entry for {mode} -- falling back to "
                f"in-memory render via display_image_with_feh"
            )
            # This shouldn't be reached for the modes we handle in
            # transition_to_mode, but if a future mode is added without
            # updating the registry, this keeps the display functional.
            return display_image_with_feh(None, mode.value, fallback_message=fallback_msg)

        img_path = get_or_render_cached(
            mode.value,
            self.screen_width,
            self.screen_height,
            render_fn,
            device_uuid,
        )

        if not img_path:
            # Rendering itself failed (PIL not available, etc.). Fall
            # back to the ImageMagick path inside display_image_with_feh.
            logger.warning(
                f"Render returned no path for {mode} -- using ImageMagick fallback"
            )
            return display_image_with_feh(None, mode.value, fallback_message=fallback_msg)

        process = display_path_with_feh(img_path)
        if process:
            logger.info(f"feh started for {mode.value}: PID {process.pid} ({img_path})")
        else:
            logger.error(f"Failed to start feh for {mode.value}")
        return process

    def transition_to_mode(self, new_mode: DisplayMode):
        """Transition to a new display mode."""
        if new_mode == self.current_mode:
            return

        old_mode = self.current_mode
        logger.info(f"Transitioning from {old_mode} to {new_mode}")

        # Clean up old mode
        if old_mode == DisplayMode.PLAYING_CONTENT:
            if self.mpv:
                self.mpv.stop_mpv()
                self.mpv = None
            self.is_playing = False
        else:
            # Kill feh if we were showing a static screen
            kill_feh_processes()
            if self.feh_process:
                try:
                    self.feh_process.terminate()
                except:
                    pass
                self.feh_process = None

        # Enter new mode
        self.current_mode = new_mode
        device_uuid = get_device_uuid()

        if new_mode == DisplayMode.AWAITING_NETWORK:
            logger.info("Showing AWAITING_NETWORK screen (setup/QR code)")
            self.feh_process = self._show_cached_screen(
                new_mode, device_uuid,
                fallback_msg="JAM Player\n\nSet up with JAM Player Setup App\nScan QR code to begin"
            )
            sd_notifier.notify("STATUS=Awaiting network (setup)")

        elif new_mode == DisplayMode.AWAITING_REGISTRATION:
            logger.info("Showing AWAITING_REGISTRATION screen")
            self.feh_process = self._show_cached_screen(
                new_mode, device_uuid,
                fallback_msg=(
                    "Your JAM Player is connected to the internet.\n\n"
                    "Set up this JAM Player in the\n"
                    "JAM Player Setup app on your phone.\n\n"
                    "Scan the QR code to begin."
                )
            )
            sd_notifier.notify("STATUS=Online, awaiting registration")

        elif new_mode == DisplayMode.AWAITING_SCREEN_LINK:
            logger.info("Showing AWAITING_SCREEN_LINK screen")
            self.feh_process = self._show_cached_screen(
                new_mode, device_uuid,
                fallback_msg=(
                    "Registered!\n\n"
                    "Almost there.\n\n"
                    "Your JAM Player is registered to your outlet.\n\n"
                    "Link this JAM Player to a screen\n"
                    "using the mobile app or web app."
                )
            )
            sd_notifier.notify("STATUS=Registered, awaiting screen link")

        elif new_mode == DisplayMode.DOWNLOADING_CONTENT:
            logger.info("Showing DOWNLOADING_CONTENT screen")
            self.feh_process = self._show_cached_screen(
                new_mode, device_uuid,
                fallback_msg="Waiting for content...\n\nContent is being downloaded.\nThis may take a few minutes."
            )
            sd_notifier.notify("STATUS=Downloading content")

        elif new_mode == DisplayMode.NO_ACTIVE_SCENES:
            logger.info("Showing NO_ACTIVE_SCENES screen")
            self.feh_process = self._show_cached_screen(
                new_mode, device_uuid,
                fallback_msg=(
                    "No active scenes\n\n"
                    "This screen has no active scenes.\n"
                    "Add scenes in the web app to display content here."
                )
            )
            sd_notifier.notify("STATUS=Screen linked but no active scenes")

        elif new_mode == DisplayMode.OUTLET_INACTIVE:
            logger.info("Showing OUTLET_INACTIVE screen")
            # Rendered inline (not cached) because the rendered content
            # depends on the cached outlet name + status label, which
            # the cache key doesn't include. See build_display_render_registry.
            img = create_outlet_inactive_screen(
                self.screen_width, self.screen_height, device_uuid,
            )
            self.feh_process = display_image_with_feh(
                img, img_name="jam_display_outlet_inactive",
                fallback_message=(
                    "Outlet inactive\n\n"
                    "This outlet is not currently active.\n"
                    "Reactivate this outlet in the web app\n"
                    "to resume content playback."
                ),
            )
            sd_notifier.notify("STATUS=Outlet inactive")

        elif new_mode == DisplayMode.PLAYING_CONTENT:
            logger.info("Entering PLAYING_CONTENT mode")
            self._start_video_playback()
            sd_notifier.notify("STATUS=Playing content")

    def _sleep_with_watchdog(self, seconds: float) -> None:
        """
        Sleep, pinging the systemd watchdog throughout, and cut the wait short
        if we are shutting down.

        EVERY wait on a path that cannot make progress must go through this.
        The unit is WatchdogSec=60 with StartLimitBurst=5/300: a path that
        waits (or spins) without pinging is killed after a minute, restarted
        into the same state, and left `failed` after five rounds -- with the
        screen dark and no way back without a human.
        """
        remaining = max(0.0, float(seconds))
        while remaining > 0 and self.running:
            sd_notifier.notify("WATCHDOG=1")
            slice_sec = min(WATCHDOG_SLEEP_SLICE_SEC, remaining)
            time.sleep(slice_sec)
            remaining -= slice_sec
        sd_notifier.notify("WATCHDOG=1")

    def _log_display_trouble(self, key: str, message: str) -> None:
        """
        Log at ERROR, at most once per DISPLAY_TROUBLE_LOG_INTERVAL_SEC PER KEY.

        The key separates unrelated faults so one cannot hide another in the
        backend's Logs & Errors panel.

        Deliberately a log line and NOT a direct report_error() call: that
        helper can block for up to 30 s on a bad network, and this runs inside
        a loop guarded by WatchdogSec=60. ERROR records are shipped to the
        backend by the logging pipeline, so the fleet signal is the same
        without putting an HTTP timeout in the display path.
        """
        now = time.time()
        if now - self._display_trouble_log_sec.get(key, 0.0) < DISPLAY_TROUBLE_LOG_INTERVAL_SEC:
            logger.debug(f"(throttled:{key}) {message}")
            return
        self._display_trouble_log_sec[key] = now
        logger.error(message)

    def _note_mpv_crash(self) -> int:
        """
        Record that an mpv process we started has exited, log a burst, and
        return how many exits fall inside the window so the caller can slow a
        start-die loop down.

        THIS USED TO RUN `systemctl restart lightdm`. It must never do that
        again. History, from the repo:

          * 2026-04-16 (07c3707) added the burst counter and the lightdm
            restart, on the theory that repeated mpv exits meant a broken
            display session that reinitialising lightdm would fix.
          * 2026-05-28 (a2a099d) root-caused the customer-visible flashing and
            black screens to lightdm restart CYCLES and shipped
            systemd/lightdm.service.d/jam-no-restart.conf (Restart=no),
            because lightdm on this image has a GObject teardown crash that
            fires deterministically on the autologin -> greeter handoff.
            On a healthy player lightdm is EXPECTED to sit `failed`; the X
            session from its one successful start survives and mpv keeps
            rendering against the DRM surface. That is what keeps the picture
            stable.
          * 2026-05-29 (7440d2d) removed the same restart from
            jam_display_hotplug_monitor and documented the mechanism: the
            restart spawns a new X session that takes DRM master from mpv;
            lightdm then hits its crash and dies; in the window before the
            master is released mpv cannot present frames, and on a
            static-image scene nothing forces a render retry, so the screen
            stays black until someone power-cycles the player.

        The copy here was simply missed. Recovery for a dead mpv is starting
        mpv again, which the caller does; a burst is recorded so the fleet
        view shows the player needs attention.
        """
        now = time.time()
        self._mpv_crash_times.append(now)
        self._mpv_crash_times = [
            t for t in self._mpv_crash_times
            if now - t < self._mpv_crash_window_seconds
        ]
        recent = len(self._mpv_crash_times)
        if recent >= self._mpv_crash_threshold:
            # The list is NOT cleared here: it prunes itself to the window
            # above, and keeping it is what lets the caller keep throttling for
            # as long as the burst lasts. _log_display_trouble is what stops
            # this becoming a log flood.
            self._log_display_trouble(
                'mpv_crash_burst',
                f"MPV exited {recent} times in "
                f"{self._mpv_crash_window_seconds}s -- the display session may be "
                f"unhealthy. NOT restarting lightdm (that causes permanent black "
                f"screens on this image); continuing to restart mpv."
            )
        return recent

    def _start_video_playback(self) -> str:
        """
        Start mpv on the first playable scheduled scene.

        Returns one of the PLAYBACK_* constants; see their definition for why
        this is not a bool. self.mpv is assigned ONLY when a process is really
        running: it used to be assigned before four early returns, so a missing
        media file (or an mpv that would not start) left a handle with no
        process behind it, and the playback loop reads
        `self.mpv and not self.mpv.is_running()` as "mpv crashed" -- an endless
        burst of phantom crashes that restarted lightdm every few seconds and
        let the watchdog kill the unit.

        PLAYBACK_NOTHING_SCHEDULED is NOT a failure. It is the ordinary state
        of a venue outside its opening hours, and the caller must route it to
        the schedule-wait screen rather than to any retry path.
        """
        # Kill any feh processes first
        kill_feh_processes()

        # Get rotation from device orientation setting
        rotation = get_rotation_angle()

        scenes = self._load_scenes()
        if not scenes:
            logger.info("Nothing scheduled to play right now")
            return PLAYBACK_NOTHING_SCHEDULED

        # mpv must start ON a file (idle mode does not display). Take the first
        # SCHEDULED scene whose file is actually on disk rather than insisting
        # on scenes[0]. The manifest and its media are swapped in atomically and
        # a scene whose download failed is never published, so this is defence
        # against out-of-band loss -- a deleted or corrupted file, a bad card --
        # not against a normal partial fetch.
        media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
        playable = []
        for scene in scenes:
            media_file = scene.get('media_file')
            if not media_file:
                continue
            try:
                candidate = media_dir / media_file
                if candidate.exists():
                    playable.append(str(candidate))
            except (TypeError, ValueError, OSError) as e:
                # A malformed media_file from the backend must skip one scene,
                # never take the service down: an uncaught raise here reaches
                # run()'s bare try and exits the process, and five of those
                # inside 300 s leave the unit `failed` with a dark screen.
                logger.error(f"Unusable media_file on scene {scene.get('id')}: {e}")
        if not playable:
            logger.error(
                f"No playable media file among {len(scenes)} scheduled scene(s) "
                f"in {media_dir}"
            )
            return PLAYBACK_NO_PLAYABLE_FILE

        initial_file = playable[0]
        # Loop when only ONE scene is actually playable, not merely when one is
        # scheduled: with a single playable file among several scheduled scenes
        # the wall-clock loop cannot switch away, and without looping mpv sits
        # on a frozen last frame (it runs with --keep-open=yes).
        single_scene = len(playable) == 1
        mpv = MpvIpcClient()
        started = mpv.start_mpv(rotation_angle=rotation, loop=single_scene, initial_file=initial_file)
        if not started and mpv.is_running():
            # start_mpv only waits 5 s for the IPC socket and does not check
            # whether the process died. On a slow cold boot the process is
            # alive and the socket is moments away, so adopt it: is_running()
            # is what the playback loop tests, and load_file reconnects when
            # the socket appears (_connect logs and returns False, it does not
            # raise). Dropping it here would orphan a fullscreen mpv that
            # nothing can ever kill, and the next attempt would unlink its
            # socket and spawn a second one to fight it for the screen.
            logger.warning("MPV socket slow to appear; adopting the running process")
            started = True
        if not started:
            # Never leave behind a process we are not going to track.
            try:
                mpv.stop_mpv()
            except Exception as e:
                logger.warning(f"Error cleaning up the MPV we could not start: {e}")
            logger.error("Failed to start MPV")
            return PLAYBACK_MPV_FAILED

        self.mpv = mpv
        logger.info(f"MPV started with initial file: {initial_file}")
        self.is_playing = True
        return PLAYBACK_STARTED

    # =========================================================================
    # Wall Clock Sync Methods
    # =========================================================================

    def _get_wall_clock_ms(self) -> int:
        """Get current wall clock time in milliseconds since epoch."""
        return int(time.time() * 1000)

    def _calculate_cycle_duration_ms(self, scenes: list) -> int:
        """Calculate total cycle duration in milliseconds."""
        total_ms = 0
        for scene in scenes:
            # All content is video now - backend provides exact duration
            duration_sec = scene.get('actual_duration', scene.get('duration', 15))
            total_ms += int(duration_sec * 1000)
        return total_ms

    def _get_scene_at_position(self, scenes: list, position_ms: int) -> tuple:
        """
        Given a position in the cycle, determine which scene and position within it.

        Returns:
            (scene_index, position_within_scene_ms, scene)
        """
        elapsed_ms = 0
        for i, scene in enumerate(scenes):
            duration_sec = scene.get('actual_duration', scene.get('duration', 15))
            duration_ms = int(duration_sec * 1000)

            if elapsed_ms + duration_ms > position_ms:
                # This is the scene we should be on
                position_within = position_ms - elapsed_ms
                return (i, position_within, scene)

            elapsed_ms += duration_ms

        # Shouldn't happen if position_ms < cycle_duration, but fallback
        return (0, 0, scenes[0])

    def _calculate_expected_position(self, cycle_duration_ms: int) -> int:
        """Calculate where in the cycle we should be based on wall clock."""
        wall_clock_ms = self._get_wall_clock_ms()
        return wall_clock_ms % cycle_duration_ms

    def _get_sync_offset_ms(self, expected_ms: int, actual_ms: int, duration_ms: int) -> int:
        """
        Calculate offset between actual and expected position.

        Returns:
            Positive = actual is AHEAD (need to slow down)
            Negative = actual is BEHIND (need to speed up)
        """
        offset_ms = actual_ms - expected_ms

        # Handle wrap-around near loop boundary
        if offset_ms > duration_ms / 2:
            offset_ms = offset_ms - duration_ms
        elif offset_ms < -duration_ms / 2:
            offset_ms = offset_ms + duration_ms

        return offset_ms

    def _adjust_video_sync(self, scene_duration_ms: int, position_in_scene_ms: int) -> None:
        """
        Adjust video playback speed based on sync offset.
        Uses proportional control - bigger offset = bigger correction.
        """
        actual_sec = self.mpv.get_playback_time()
        if actual_sec is None:
            return

        actual_ms = int(actual_sec * 1000)
        offset_ms = self._get_sync_offset_ms(position_in_scene_ms, actual_ms, scene_duration_ms)
        abs_offset = abs(offset_ms)

        if abs_offset > SEEK_THRESHOLD_MS:
            # Emergency seek required
            target_sec = position_in_scene_ms / 1000.0
            logger.warning(f"SYNC EMERGENCY SEEK: offset={offset_ms}ms, seeking to {target_sec:.2f}s")
            self.mpv.seek(target_sec)
            self.mpv.set_speed(SPEED_NORMAL)
            self._current_speed = SPEED_NORMAL

        elif abs_offset > 100:
            # Aggressive correction (100-500ms)
            new_speed = SPEED_AGGRESSIVE_FAST if offset_ms < 0 else SPEED_AGGRESSIVE_SLOW
            if new_speed != getattr(self, '_current_speed', SPEED_NORMAL):
                self.mpv.set_speed(new_speed)
                self._current_speed = new_speed

        elif abs_offset > 30:
            # Moderate correction (30-100ms)
            new_speed = SPEED_MODERATE_FAST if offset_ms < 0 else SPEED_MODERATE_SLOW
            if new_speed != getattr(self, '_current_speed', SPEED_NORMAL):
                self.mpv.set_speed(new_speed)
                self._current_speed = new_speed

        elif abs_offset > TARGET_SYNC_TOLERANCE_MS:
            # Gentle correction (10-30ms)
            new_speed = SPEED_GENTLE_FAST if offset_ms < 0 else SPEED_GENTLE_SLOW
            if new_speed != getattr(self, '_current_speed', SPEED_NORMAL):
                self.mpv.set_speed(new_speed)
                self._current_speed = new_speed

        else:
            # In sync - normal speed
            if getattr(self, '_current_speed', SPEED_NORMAL) != SPEED_NORMAL:
                self.mpv.set_speed(SPEED_NORMAL)
                self._current_speed = SPEED_NORMAL

    def _preload_video_durations(self, scenes: list, media_dir: Path) -> list:
        """
        Set actual_duration for all scenes.

        The backend now provides exact video durations via the 'duration' field,
        so we just copy that to 'actual_duration'. No ffprobe needed.

        All content is now video (images are converted to video by backend).
        """
        for scene in scenes:
            # Backend provides exact duration - no need to probe
            scene['actual_duration'] = scene.get('duration', 15)

        return scenes

    # =========================================================================
    # Main Content Loop with Wall Clock Sync
    # =========================================================================

    def run_video_loop(self):
        """
        Main content playback loop with wall clock synchronization.

        Plays scenes one-by-one with wall clock sync. All JAM Players displaying
        the same Screen will show the same content at the same time, synchronized
        via wall clock (chrony/NTP).
        """
        # Ensure MPV is running - restart if it died
        if not self.mpv or not self.mpv.is_running():
            logger.info("MPV not running, (re)starting video playback...")
            if self.mpv:
                try:
                    self.mpv.stop_mpv()
                except Exception as e:
                    logger.warning(f"Error cleaning up MPV: {e}")
                self.mpv = None
                self.is_playing = False
            outcome = self._start_video_playback()
            if outcome == PLAYBACK_NOTHING_SCHEDULED:
                # NOT a failure, and it must NOT return here. Everything is
                # scheduled off right now, and _run_scene_by_scene_sync owns
                # that case: it puts the branded "No Content Scheduled" screen
                # up and polls until the schedule opens. Returning instead
                # would leave the customer looking at a bare desktop for the
                # whole off-schedule window -- every venue with opening hours,
                # every night after the 3 AM reboot -- because
                # _start_video_playback has already run kill_feh_processes().
                logger.info(
                    "Nothing scheduled right now -- handing over to the "
                    "schedule-wait screen"
                )
            elif outcome != PLAYBACK_STARTED:
                # A real fault: no playable file, or mpv will not start. Do not
                # return straight back into the main loop, which calls this
                # method again immediately (`run_video_loop(); continue`) and
                # would spin with no watchdog ping at all. Wait here, pinging
                # throughout, then return so the main loop re-evaluates the
                # display mode (the answer may now be DOWNLOADING_CONTENT).
                self._log_display_trouble(
                    'mpv_start',
                    f"Display cannot start MPV ({outcome}). Retrying; the "
                    f"service stays up."
                )
                self._sleep_with_watchdog(STATE_CHECK_INTERVAL_SEC)
                return
            else:
                self._mpv_restart_backoff_sec = MPV_RESTART_BACKOFF_MIN_SEC

        logger.info("=" * 60)
        logger.info("Starting SYNCED content playback (wall clock mode)")
        logger.info(f"Sync config: check={SYNC_CHECK_INTERVAL_MS}ms, tolerance={TARGET_SYNC_TOLERANCE_MS}ms")
        logger.info("=" * 60)

        self._run_scene_by_scene_sync()

    def _show_no_scheduled_content_screen(self):
        """
        Show the "No Content Scheduled" message when scenes exist but
        all are scheduled off for the current day/time.

        Uses the display cache (get_or_render_cached). jam-update
        pre-warms a PNG at install time so this transition is
        near-instant. If the cache miss path fires (e.g. first
        post-update display before pre-warm has run, or commit hash
        mismatch), falls back to display_image_with_feh's
        ImageMagick fallback path -- but the freshly-rendered PIL
        path is avoided here entirely. See the 2026-05-24 incident
        where rendering inline at 4K caused ~10-15s of bare-desktop
        before this screen appeared.
        """
        logger.info("All scenes scheduled off - showing 'no content scheduled' message")

        # Stop MPV if running -- it's holding the screen with content
        # that's no longer scheduled to play.
        if self.mpv:
            self.mpv.stop_mpv()
            self.mpv = None

        device_uuid = get_device_uuid()
        img_path = get_or_render_cached(
            NO_SCHEDULED_CONTENT_CACHE_KEY,
            self.screen_width,
            self.screen_height,
            create_no_scheduled_content_screen,
            device_uuid,
        )

        if img_path:
            self.feh_process = display_path_with_feh(img_path)
        else:
            # Cache + render both failed (PIL unavailable, etc). Fall
            # through to display_image_with_feh's ImageMagick fallback
            # so the customer at least sees text instead of a black
            # screen.
            logger.warning(
                "Render returned no path for no_scheduled_content -- "
                "using ImageMagick fallback"
            )
            self.feh_process = display_image_with_feh(
                None,
                "jam_display_no_schedule",
                fallback_message="No Content Scheduled\n\nContent will appear\nduring scheduled hours.",
            )

    def _get_num_screens(self) -> Optional[int]:
        """Layout screen count, written next to scenes.json by
        scenes_manager. None if the backend/scenes_manager didn't provide it
        -> treated as single-screen (no sync), so this is fully backward
        compatible with an older backend or a device mid-rollout."""
        try:
            f = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "num_screens.txt"
            if f.exists():
                return int(f.read_text().strip())
        except (ValueError, OSError):
            pass
        return None

    def _clock_is_synced(self) -> bool:
        """check_chrony_sync() cached + re-polled every WALL_SYNC_CLOCK_RECHECK_S.
        Avoids shelling out to chronyc on every scene load, and lets a
        late-converging clock start being trusted without a restart."""
        now = time.monotonic()
        if now - self._clock_checked_at >= WALL_SYNC_CLOCK_RECHECK_S:
            self._clock_checked_at = now
            try:
                self._clock_synced = check_chrony_sync()
            except Exception:
                self._clock_synced = False
        return self._clock_synced

    def _wall_sync_active(self) -> bool:
        """Master gate for wall-clock seek-on-load. ALL must hold:
          - not force-disabled by the kill-switch file
          - the layout has >1 screen (a single screen NEVER syncs -- its
            playback stays byte-identical to the legacy bare load)
          - the shared clock has converged (seeking to a wrong wall clock
            would be strictly worse than not seeking)
        Media-type (VIDEO only) and the offset threshold are checked at the
        seek site. Every failing gate falls back to today's behavior."""
        try:
            if WALL_SYNC_KILL_SWITCH.exists():
                return False
        except OSError:
            pass
        n = self._num_screens
        if n is None or n <= 1:
            return False
        return self._clock_is_synced()

    def _run_scene_by_scene_sync(self):
        """
        Play scenes one by one with wall clock sync.
        Supports dynamic scheduling - periodically re-filters scenes by day/time.
        """
        media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
        scenes = self._load_scenes()
        # Layout size for this content, read once per entry (it only changes on
        # a relink, which re-enters this loop). Gates whether we wall-sync.
        self._num_screens = self._get_num_screens()

        if not scenes:
            # _load_scenes() returned empty. Two possible causes (same
            # distinction as in run_video_loop -- see comments at the
            # other call site for details):
            #   (a) Scenes exist on disk but all are scheduled off now.
            #   (b) scenes.json is empty (customer deactivated all scenes).
            # Show the "no scheduled content" screen ONLY for case (a).
            # For case (b), bail to the main loop so it transitions to
            # the proper NO_ACTIVE_SCENES mode -- otherwise the customer
            # sees "Content will appear during scheduled hours" when
            # they actually have no content configured at all.
            unfiltered_check = self._load_scenes(apply_schedule_filter=False)
            if not unfiltered_check:
                logger.info(
                    "No scenes on disk - exiting PLAYING_CONTENT so main "
                    "loop can transition to NO_ACTIVE_SCENES"
                )
                # Clean up display so the cached NO_ACTIVE_SCENES screen
                # can take over cleanly on the next main-loop tick.
                kill_feh_processes()
                if self.feh_process:
                    try:
                        self.feh_process.terminate()
                    except:
                        pass
                    self.feh_process = None
                return

            logger.warning("No scenes loaded (all may be scheduled off)")
            # Show "no scheduled content" screen instead of black
            self._show_no_scheduled_content_screen()
            # Wait for schedule to potentially change
            while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
                # Ten seconds of bare time.sleep here meant no watchdog ping:
                # the unit was SIGABRT'd about every 65 s for the whole of an
                # off-schedule window, and five kills inside 300 s left it
                # `failed` -- black until a human intervened.
                self._sleep_with_watchdog(10)
                # Bail-out check on every tick: if scenes.json went
                # empty while we were waiting (customer deactivated all),
                # exit to the main loop so it can transition to
                # NO_ACTIVE_SCENES. Without this we'd sit here showing
                # "no content scheduled" indefinitely while the real
                # state is "no content at all."
                unfiltered_check = self._load_scenes(apply_schedule_filter=False)
                if not unfiltered_check:
                    logger.info(
                        "Scenes removed while waiting - exiting to "
                        "re-evaluate mode"
                    )
                    kill_feh_processes()
                    if self.feh_process:
                        try:
                            self.feh_process.terminate()
                        except:
                            pass
                        self.feh_process = None
                    return

                # Re-check if any scenes are now scheduled
                scenes = self._load_scenes()
                if scenes:
                    logger.info(f"Scenes now scheduled - resuming playback with {len(scenes)} scenes")
                    # Kill feh before restarting MPV
                    kill_feh_processes()
                    if self.feh_process:
                        try:
                            self.feh_process.terminate()
                        except:
                            pass
                        self.feh_process = None
                    # Restart MPV for playback
                    self._start_video_playback()
                    break
            else:
                # Mode changed or stopped, just return
                return

        # Get actual video durations (use duration from API, backend provides exact values now)
        # No need to probe with ffprobe - backend ensures exact durations
        for scene in scenes:
            if 'actual_duration' not in scene:
                scene['actual_duration'] = scene.get('duration', 15)

        cycle_duration_ms = self._calculate_cycle_duration_ms(scenes)
        logger.info(f"Loaded {len(scenes)} active scenes, cycle duration: {cycle_duration_ms}ms ({cycle_duration_ms/1000:.1f}s)")

        self._current_speed = SPEED_NORMAL
        self._current_scene_index = -1
        # (scene id, media path) pairs already reported missing, so a missing
        # file is one ERROR rather than two per second. See the playback loop.
        self._missing_media_reported = set()
        self._last_sync_check = 0
        self._last_sync_log = 0
        self._last_schedule_check = 0

        # How often to re-check schedule (every 60 seconds)
        SCHEDULE_CHECK_INTERVAL_SEC = 60

        while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
            current_time_sec = time.time()

            # Check for content updates (file modified)
            scenes_file = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "scenes.json"
            try:
                scenes_mtime = scenes_file.stat().st_mtime if scenes_file.exists() else 0
            except:
                scenes_mtime = 0

            content_changed = not hasattr(self, '_last_scenes_mtime') or scenes_mtime != self._last_scenes_mtime

            # Periodically re-check schedule even if content hasn't changed
            schedule_check_needed = (current_time_sec - self._last_schedule_check) >= SCHEDULE_CHECK_INTERVAL_SEC

            if content_changed or schedule_check_needed:
                if content_changed:
                    self._last_scenes_mtime = scenes_mtime
                    # Re-read the layout screen count. num_screens.txt is swapped
                    # into LIVE atomically with scenes.json, so a changed scenes
                    # mtime guarantees this value is fresh too -- no extra polling.
                    #
                    # Without this, _num_screens is only read once per entry into
                    # this method, so a layout resized IN PLACE (same screen, a
                    # screen added to or removed from the layout) leaves a stale
                    # count until the display restarts: wall sync would never
                    # engage on a newly-multi-screen layout, or would keep seeking
                    # on a screen that has since become standalone.
                    self._num_screens = self._get_num_screens()
                    logger.info("Content file updated, reloading scenes")
                if schedule_check_needed:
                    self._last_schedule_check = current_time_sec
                    logger.debug("Periodic schedule re-check")

                new_scenes = self._load_scenes()  # This applies schedule filter

                if new_scenes:
                    # Update durations
                    for scene in new_scenes:
                        if 'actual_duration' not in scene:
                            scene['actual_duration'] = scene.get('duration', 15)

                    # Check if scene list changed (IDs or media files) - used to determine if we reset playback
                    old_scene_keys = [(s.get('id'), s.get('media_file')) for s in scenes]
                    new_scene_keys = [(s.get('id'), s.get('media_file')) for s in new_scenes]
                    scene_list_changed = old_scene_keys != new_scene_keys

                    # Always update scenes when content file changed (catches duration/metadata changes)
                    if content_changed or scene_list_changed:
                        scenes = new_scenes
                        cycle_duration_ms = self._calculate_cycle_duration_ms(scenes)
                        if scene_list_changed:
                            # Only reset playback position when actual scenes changed
                            self._current_scene_index = -1
                            logger.info(f"Scene list changed: {len(scenes)} scenes, cycle: {cycle_duration_ms}ms")
                        else:
                            logger.info(f"Scene metadata updated (duration, etc): cycle now {cycle_duration_ms}ms")
                elif not new_scenes and scenes:
                    # Filtered scene list went from non-empty to empty. Two
                    # possible causes:
                    #   (a) Scenes still exist on disk but ALL are scheduled
                    #       off right now (e.g. day-of-week / time-of-day
                    #       filter excluded everything). In this case we
                    #       want to show our "no content scheduled" message
                    #       and stay in PLAYING_CONTENT so the next loop
                    #       iteration can pick scenes back up the moment
                    #       the schedule allows.
                    #   (b) The backend returned zero scenes for this device
                    #       (customer deactivated everything in the web app)
                    #       and scenes.json was overwritten to []. In this
                    #       case the underlying state is NO_ACTIVE_SCENES;
                    #       we should bail to the main loop and let it
                    #       transition to that mode (which uses a
                    #       pre-rendered cached PNG and is near-instant).
                    #
                    # Distinguish via _load_scenes(apply_schedule_filter=False).
                    # Important: we must NOT call _show_no_scheduled_content_screen()
                    # in case (b) -- that helper renders a fresh mesh-gradient
                    # PNG at runtime, which takes ~10-15s at 4K and leaves
                    # the customer staring at the bare desktop while it
                    # works. The NO_ACTIVE_SCENES cached screen renders
                    # near-instantly from disk by comparison.
                    unfiltered_check = self._load_scenes(apply_schedule_filter=False)
                    if not unfiltered_check:
                        # Case (b) -- bail to main loop; the empty `scenes`
                        # below will trigger the bail-out at line ~2538.
                        logger.info(
                            "All scenes removed from backend - "
                            "exiting PLAYING_CONTENT so main loop can "
                            "transition to NO_ACTIVE_SCENES"
                        )
                    else:
                        # Case (a) -- show the schedule-off message and
                        # stay in the loop to wait for the schedule to
                        # allow playback again.
                        logger.info("All scenes now scheduled off - showing waiting screen")
                        self._show_no_scheduled_content_screen()
                    scenes = []
                    self._current_scene_index = -1

            if not scenes:
                # Distinguish "no content at all on disk" from "content exists
                # but is scheduled off right now". _load_scenes() above used
                # the day/time schedule filter; re-load WITHOUT the filter to
                # check whether the underlying scenes.json has any scenes at
                # all. If it doesn't, we shouldn't be in PLAYING_CONTENT --
                # bail so the main loop transitions us to NO_ACTIVE_SCENES
                # or DOWNLOADING_CONTENT as appropriate. If it does, we just
                # need to wait until the schedule permits playback again.
                unfiltered_scenes = self._load_scenes(apply_schedule_filter=False)
                if not unfiltered_scenes:
                    logger.info("No content available - exiting to re-evaluate mode")
                    # Clean up display
                    kill_feh_processes()
                    if self.feh_process:
                        try:
                            self.feh_process.terminate()
                        except:
                            pass
                        self.feh_process = None
                    return  # Exit to main loop to re-check mode

                # Wait and re-check for scheduled scenes
                sd_notifier.notify("WATCHDOG=1")
                time.sleep(5)
                new_scenes = self._load_scenes()
                if new_scenes:
                    logger.info(f"Scenes now scheduled - resuming playback with {len(new_scenes)} scenes")
                    # Kill feh and restart MPV
                    kill_feh_processes()
                    if self.feh_process:
                        try:
                            self.feh_process.terminate()
                        except:
                            pass
                        self.feh_process = None
                    self._start_video_playback()
                    scenes = new_scenes
                    for scene in scenes:
                        if 'actual_duration' not in scene:
                            scene['actual_duration'] = scene.get('duration', 15)
                    cycle_duration_ms = self._calculate_cycle_duration_ms(scenes)
                    self._current_scene_index = -1
                continue

            # No picture: either the mpv we started has exited, or there is no
            # mpv at all because the last start failed. Both are handled the
            # same way -- start mpv again, with a backoff when it will not
            # start. NEVER by restarting lightdm; see _note_mpv_crash.
            if self.mpv is None or not self.mpv.is_running():
                recent_crashes = 0
                if self.mpv is not None:
                    logger.warning("MPV process died, restarting...")
                    recent_crashes = self._note_mpv_crash()
                    try:
                        self.mpv.stop_mpv()
                    except Exception as e:
                        logger.warning(f"Error stopping dead MPV: {e}")
                    self.mpv = None
                    self.is_playing = False

                if recent_crashes >= 2:
                    # mpv keeps starting and dying. Wait BEFORE trying again,
                    # so that once it does start the loop immediately corrects
                    # the wall-clock position instead of showing the bootstrap
                    # scene for the length of the wait. The FIRST exit never
                    # waits: one-off crash recovery stays as immediate as it
                    # has always been.
                    self._sleep_with_watchdog(
                        min(recent_crashes, MPV_CRASH_BURST_MAX_WAIT_SEC)
                    )

                outcome = self._start_video_playback()
                if outcome == PLAYBACK_STARTED:
                    self._mpv_restart_backoff_sec = MPV_RESTART_BACKOFF_MIN_SEC
                    self._current_scene_index = -1  # Force scene reload
                    sd_notifier.notify("WATCHDOG=1")
                elif outcome == PLAYBACK_NOTHING_SCHEDULED:
                    # The schedule closed underneath us. Leave the loop so the
                    # main loop re-enters and the block above puts the
                    # "No Content Scheduled" screen up.
                    logger.info("Schedule closed -- leaving playback for the wait screen")
                    return
                else:
                    self._log_display_trouble(
                        'mpv_start',
                        f"Display cannot start MPV ({outcome}). Retrying; the "
                        f"service stays up."
                    )
                    self._sleep_with_watchdog(self._mpv_restart_backoff_sec)
                    self._mpv_restart_backoff_sec = min(
                        self._mpv_restart_backoff_sec * 2, MPV_RESTART_BACKOFF_MAX_SEC
                    )
                continue

            # Calculate where we should be based on wall clock

            position_in_cycle_ms = self._calculate_expected_position(cycle_duration_ms)
            scene_index, position_in_scene_ms, scene = self._get_scene_at_position(scenes, position_in_cycle_ms)

            media_file = scene.get('media_file')
            media_type = scene.get('media_type', 'IMAGE')  # IMAGE or VIDEO
            scene_duration_ms = int(scene.get('actual_duration', scene.get('duration', 15)) * 1000)
            media_path = media_dir / media_file

            # Check if we need to switch scenes
            if scene_index != self._current_scene_index:
                if not media_path.exists():
                    # This loop is wall-clock driven: it recomputes the scene
                    # that SHOULD be showing every 0.5 s, so a missing file
                    # used to log an ERROR twice a second for the whole of
                    # that scene's slot (7200/h, all of it written to the SD
                    # card). Report each missing file once and stay quiet
                    # until it changes; the screen keeps showing the previous
                    # scene and the schedule moves on by itself.
                    missing_key = (scene.get('id'), str(media_path))
                    if missing_key not in self._missing_media_reported:
                        self._missing_media_reported.add(missing_key)
                        logger.error(f"Media file not found: {media_path}")
                    else:
                        logger.debug(f"Media file still missing: {media_path}")
                    # Ping before sleeping: a scene whose file is missing holds
                    # this branch for the whole of its scheduled slot, which is
                    # longer than WatchdogSec on most schedules.
                    sd_notifier.notify("WATCHDOG=1")
                    # Forget which scene is loaded. Without this, a cycle whose
                    # only playable scene has already been loaded matches
                    # `scene_index == self._current_scene_index` when its slot
                    # comes round again, the reload is skipped, and mpv sits on
                    # a frozen last frame forever (it runs with --keep-open).
                    self._current_scene_index = -1
                    time.sleep(0.5)
                    continue

                logger.debug(f"Switching to scene {scene_index}: {scene.get('id')} ({media_type})")
                self._current_scene_index = scene_index

                # Load the scene. On a multi-screen wall (and only then) seek
                # to the wall-clock offset so a screen entering this scene late
                # jumps to where its peers already are, instead of playing from
                # t=0 and staying desynced every cycle. Single-screen layouts,
                # image scenes, an unconverged clock, or sub-threshold offsets
                # all fall through to the legacy bare load -- byte-identical to
                # before this change.
                self.mpv.load_file(str(media_path))
                if (media_type == 'VIDEO'
                        and position_in_scene_ms >= WALL_SYNC_MIN_SEEK_MS
                        and self._wall_sync_active()):
                    self.mpv.seek(position_in_scene_ms / 1000.0, exact=True)
                    logger.info(
                        f"Wall sync: seeked scene {scene_index} to "
                        f"{position_in_scene_ms / 1000.0:.2f}s "
                        f"(numScreens={self._num_screens})"
                    )
                time.sleep(0.1)  # Brief delay for MPV to initialize

                # For single-scene content, ensure looping is enabled
                # (loadfile replace can reset the loop property)
                if len(scenes) == 1:
                    self.mpv.set_property('loop-file', 'inf')
                    logger.debug("Single scene - enabled loop-file=inf")

            # Notify systemd watchdog
            sd_notifier.notify("WATCHDOG=1")
            time.sleep(0.05)

    def _check_content_updated(self) -> bool:
        """Check if scenes.json has been updated since we last loaded it."""
        scenes_file = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "scenes.json"
        if not scenes_file.exists():
            return False
        current_mtime = scenes_file.stat().st_mtime
        return hasattr(self, '_last_scenes_mtime') and current_mtime != self._last_scenes_mtime

    def _wait_for_video_end(self) -> bool:
        """Wait for the current video to finish. Returns False if interrupted."""
        # Get video duration
        duration = None
        for _ in range(30):
            if not self.running or self.current_mode != DisplayMode.PLAYING_CONTENT:
                return False
            duration = self.mpv.get_duration()
            if duration is not None and duration > 0:
                break
            time.sleep(0.5)

        if duration is None:
            logger.warning("Could not get video duration, using 30s fallback")
            duration = 30

        logger.debug(f"Video duration: {duration:.1f}s")

        start_time = time.time()
        last_content_check = start_time
        while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
            current_time = time.time()

            # Check for state changes
            if current_time - start_time > 5:
                new_mode = self.determine_display_mode()
                if new_mode != self.current_mode:
                    return False

            # Check for content updates every second
            if current_time - last_content_check >= 1:
                last_content_check = current_time
                if self._check_content_updated():
                    logger.info("Content updated during video playback, interrupting")
                    return False

            # Check if video ended
            eof = self.mpv.get_property('eof-reached')
            if eof:
                logger.info("Video playback complete")
                return True

            # Safety timeout
            if current_time - start_time > duration + 5:
                logger.warning("Video timeout, moving to next scene")
                return True

            sd_notifier.notify("WATCHDOG=1")
            time.sleep(0.1)

        return False

    def _wait_for_duration(self, duration_seconds: int) -> bool:
        """Wait for the specified duration. Returns False if interrupted."""
        logger.debug(f"Displaying image for {duration_seconds}s")

        start_time = time.time()
        last_state_check = start_time
        last_content_check = start_time

        while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
            current_time = time.time()
            elapsed = current_time - start_time

            if elapsed >= duration_seconds:
                return True

            # Check for state changes periodically
            if current_time - last_state_check >= 5:
                last_state_check = current_time
                new_mode = self.determine_display_mode()
                if new_mode != self.current_mode:
                    return False

            # Check for content updates every second
            if current_time - last_content_check >= 1:
                last_content_check = current_time
                if self._check_content_updated():
                    logger.info("Content updated during image display, interrupting")
                    return False

            sd_notifier.notify("WATCHDOG=1")
            time.sleep(0.1)

        return False

    def _show_boot_identity_screen_once(self) -> None:
        """
        Put the device-identity screen up for BOOT_IDENTITY_HOLD_SECONDS, once
        per BOOT, before the first real display mode.

        Once per BOOT, not once per service start. This unit restarts on
        watchdog kills, on crashes and on every update, and re-holding the
        screen for 15 s on each of those would delay the customer's content
        exactly when the player is already struggling. The marker lives in
        /run, which the kernel clears at boot.

        Skipped -- and still marked done -- when showing it would be wrong:
          * jam-update is showing its own screen. Never fight the updater for
            the display; that is how the 2026-05 incidents started.
          * there is no device UUID yet. A freshly imaged player has nothing to
            identify, and the shipped logo splash stays in place.
          * the render or feh failed. This screen is an aid, never a gate.
        Marking on those paths is deliberate: a boot screen that turned up
        later in the session, mid-setup or mid-playback, would be worse than
        one that was missed.
        """
        try:
            if BOOT_IDENTITY_SHOWN_FLAG.exists():
                return
        except Exception:
            return  # cannot read the marker: do nothing rather than risk a re-show

        try:
            if (UPDATE_IN_PROGRESS_FLAG.exists()
                    and not _is_update_flag_stale()
                    and _jam_update_service_is_active()):
                logger.info("jam-update owns the screen; skipping the boot identity screen")
                return

            device_uuid = get_device_uuid()
            if not device_uuid:
                logger.info("No device UUID yet; skipping the boot identity screen")
                return

            img_path = get_or_render_cached(
                BOOT_IDENTITY_CACHE_KEY,
                self.screen_width,
                self.screen_height,
                create_boot_identity_screen,
                device_uuid,
            )
            if not img_path:
                logger.warning("Boot identity screen did not render; skipping")
                return

            process = display_path_with_feh(img_path)
            if not process:
                logger.warning("Could not put the boot identity screen up; skipping")
                return

            # Tracked, so the first real transition_to_mode tears it down like
            # any other static screen.
            self.feh_process = process
            logger.info(
                f"Boot identity screen up for {BOOT_IDENTITY_HOLD_SECONDS}s (PID {process.pid})"
            )
            self._sleep_with_watchdog(BOOT_IDENTITY_HOLD_SECONDS)

            # Promote to the Plymouth splash only when the render was good
            # enough to cache. A /tmp path means something was missing (no MACs
            # yet, or /var/cache is broken), and that must never become the
            # image every future boot shows.
            if str(img_path).startswith(str(DISPLAY_CACHE_DIR)):
                install_boot_splash(img_path)
            else:
                logger.info("Boot identity render was not cacheable; leaving the splash alone")
        except Exception as e:
            logger.warning(f"Boot identity screen failed (non-fatal): {e}")
        finally:
            try:
                BOOT_IDENTITY_SHOWN_FLAG.parent.mkdir(parents=True, exist_ok=True)
                BOOT_IDENTITY_SHOWN_FLAG.touch()
            except Exception as e:
                logger.debug(f"Could not mark the boot identity screen shown: {e}")

    def run(self):
        """Main run loop - monitors state and manages display modes."""
        log_service_start(logger, 'JAM Player Display Service')

        logger.info("=" * 60)
        logger.info("JAM PLAYER DISPLAY SERVICE - 4-MODE UNIFIED DISPLAY")
        logger.info(f"Screen: {self.screen_width}x{self.screen_height}")
        _log_dependency_status()
        logger.info("=" * 60)

        # Sweep the display cache once at startup. Only fires when the
        # installed commit hash has changed since the last cleanup --
        # see cleanup_stale_cache() for the short-circuit. Adds ~10ms
        # on the common no-op path.
        try:
            cleanup_stale_cache()
        except Exception as e:
            # Cache cleanup is best-effort. A failure here must not
            # block the service from coming up -- the display has to
            # work even with a dirty cache.
            logger.warning(f"Cache cleanup raised, continuing: {e}")

        # Send READY=1 immediately - we're initialized and entering main loop
        # Display availability is handled within the loop, not a startup blocker
        sd_notifier.notify("READY=1")
        # Identify the player before anything else claims the screen. Once per
        # boot, bounded, and skipped whenever it would be wrong -- see the
        # method. READY=1 goes first so systemd never waits on this.
        self._show_boot_identity_screen_once()
        logger.info("Service ready, entering main loop")

        last_state_check = 0

        try:
            while self.running:
                current_time = time.time()

                # Check state periodically (or if mode is None)
                if self.current_mode is None or current_time - last_state_check >= STATE_CHECK_INTERVAL_SEC:
                    last_state_check = current_time
                    new_mode = self.determine_display_mode()

                    if new_mode != self.current_mode:
                        # Defer state-driven transitions while jam-update is
                        # actively running and showing its "updating" screen.
                        # If we transitioned right now we'd kill jam-update's
                        # feh (via transition_to_mode's kill_feh_processes)
                        # and replace it with our own state screen, which
                        # would (a) hide the "updating" message from the
                        # customer mid-update and (b) potentially show a
                        # wrong state (e.g. NO_ACTIVE_SCENES the moment
                        # screen_id.txt is written but before content has
                        # been fetched). Leaving self.current_mode unchanged
                        # means the next iteration AFTER the flag clears
                        # will see new_mode != current_mode and transition
                        # cleanly. See the 2026-05-24 incident where the
                        # customer saw NO_ACTIVE_SCENES while jam-update
                        # was still mid-install.
                        #
                        # Stale-flag protection mirrors the feh-respawn
                        # guard below: only honor the flag if it's both
                        # young AND jam-update.service is actually active.
                        if (UPDATE_IN_PROGRESS_FLAG.exists()
                                and not _is_update_flag_stale()
                                and _jam_update_service_is_active()):
                            logger.info(
                                f"Display mode change requested ({self.current_mode} -> {new_mode}) "
                                f"but jam-update is in progress -- deferring "
                                f"transition until update completes"
                            )
                        else:
                            self.transition_to_mode(new_mode)

                # If in playing mode, run the video loop (blocking until state changes)
                if self.current_mode == DisplayMode.PLAYING_CONTENT:
                    self.run_video_loop()
                    # After video loop exits, recheck state
                    continue

                # For static display modes, check feh is still running and sleep.
                if self.feh_process:
                    poll_result = self.feh_process.poll()
                    if poll_result is not None:
                        # feh died. Normally we respawn it -- but if
                        # jam-update.service is currently running and
                        # showing the "updating" screen, it has
                        # deliberately killed our feh and replaced it
                        # with its own. Respawning ours would
                        # leapfrog jam-update's display in z-order and
                        # hide the update screen from the customer.
                        #
                        # Stay quiescent until the update completes
                        # (flag is cleared by jam_update.py's
                        # hide_updating_screen). The next main-loop
                        # iteration will then see no flag + no feh and
                        # respawn naturally.
                        #
                        # Stale-flag protection: jam-update could
                        # crash hard between writing the flag and
                        # removing it, leaving us paused forever. Two
                        # layers of protection:
                        #   1. Cross-check that jam-update.service is
                        #      actually active. The systemctl call is
                        #      a few ms and tells us truthfully whether
                        #      the service is running NOW.
                        #   2. Hard timeout via _is_update_flag_stale.
                        #      If the flag file is older than
                        #      UPDATE_FLAG_MAX_AGE_SEC, treat it as
                        #      stale regardless of what systemctl says.
                        #      This catches the case where systemctl
                        #      lies (D-Bus race, etc.) or where
                        #      jam-update genuinely gets stuck for
                        #      longer than any legitimate install
                        #      should take.
                        if (UPDATE_IN_PROGRESS_FLAG.exists()
                                and not _is_update_flag_stale()
                                and _jam_update_service_is_active()):
                            logger.info(
                                "feh died but jam-update is in progress -- "
                                "not respawning to avoid covering update screen"
                            )
                            self.feh_process = None
                        else:
                            logger.warning(
                                f"feh process exited with code {poll_result}, "
                                f"restarting display"
                            )
                            # Force re-transition to current mode to restart feh
                            old_mode = self.current_mode
                            self.current_mode = None
                            self.transition_to_mode(old_mode)

                sd_notifier.notify("WATCHDOG=1")
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.cleanup()


def main():
    manager = JamPlayerDisplayManager()
    manager.run()


if __name__ == '__main__':
    main()
