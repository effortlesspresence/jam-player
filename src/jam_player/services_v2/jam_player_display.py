#!/usr/bin/env python3
"""
JAM Player Display Service - Unified Display Manager

This service handles all display states for the JAM Player with modern,
premium gradient-based UI design.

Display Modes:

1. UNREGISTERED (not registered OR registered but not linked to screen)
   - Display: Premium setup screen with JAM logo, QR code, and "Get ready to JAM."
   - Modern dark gradient background with subtle orange glow
   - Guides user to download JAM Player Setup app

2. LINKED_WAITING_FOR_CONTENT (linked to screen, content downloading)
   - Display: "Waiting for content..." with animated-style loading indicator
   - Shows progress while content is being downloaded

3. PLAYING_CONTENT (linked and content available)
   - Display: Plays scenes sequentially from scenes.json
   - Wall clock synchronized playback for multi-display setups
   - Automatically reloads when content is updated

This service monitors state changes and transitions between display modes automatically.
"""

import sys
import os
import time
import json
import socket
import subprocess
import signal
import threading
from pathlib import Path
from enum import Enum
from typing import Optional, Any, List, Dict
from dataclasses import dataclass
from datetime import datetime, time as dt_time

# Add the services directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.logging_config import setup_service_logging, log_service_start
from common.credentials import (
    is_device_registered,
    get_device_uuid,
    get_screen_id,
    get_device_uuid_short,
    get_display_orientation,
)
from common.system import get_systemd_notifier, setup_signal_handlers
from common.paths import (
    SCREEN_ID_FILE,
    REGISTERED_FLAG,
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


class DisplayMode(Enum):
    """The 3 display modes for JAM Player."""
    UNREGISTERED = "unregistered"  # Not registered OR registered but not linked
    LINKED_WAITING_FOR_CONTENT = "linked_waiting_for_content"
    PLAYING_CONTENT = "playing_content"


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

FONT_SIZE_TITLE = 72
FONT_SIZE_SUBTITLE = 36
FONT_SIZE_INSTRUCTIONS = 32
FONT_SIZE_URL = 28
FONT_SIZE_DEVICE_ID = 24
FONT_SIZE_TAGLINE = 42

# Logo path on device
JAM_LOGO_PATH = "/root/jam_logo.png"

# URLs for setup
UNIVERSAL_SETUP_URL = "https://setup.justamenu.com"

# State checking intervals
STATE_CHECK_INTERVAL_SEC = 5


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
    return ImageFont.load_default()


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
    Create the setup screen for UNREGISTERED mode.

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

    # Fonts
    title_font = get_font(FONT_SIZE_TITLE)
    subtitle_font = get_font(FONT_SIZE_SUBTITLE)
    instructions_font = get_font(FONT_SIZE_INSTRUCTIONS, bold=False)
    tagline_font = get_font(FONT_SIZE_TAGLINE)
    device_font = get_font(FONT_SIZE_DEVICE_ID, bold=False)

    center_x = width // 2

    # Calculate layout - vertically centered content block
    logo_height = min(120, height // 8)
    qr_size = min(320, height // 4)

    # Start from top with some padding
    y = int(height * 0.08)

    # Logo
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        # Paste with alpha mask for transparency
        img.paste(logo, (logo_x, y), logo if logo.mode == 'RGBA' else None)
        y += logo.height + 30
    else:
        # Fallback: draw a simple placeholder or skip
        y += 40

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
    y += bbox[3] + 40

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
    y += bbox[3] + 20

    # "Scan the QR code to begin."
    scan_text = "Scan the QR code to begin."
    draw.text(
        (center_x, y),
        scan_text,
        font=instructions_font,
        fill=SECONDARY_COLOR,
        anchor="mt"
    )
    bbox = draw.textbbox((0, 0), scan_text, font=instructions_font)
    y += bbox[3] + 40

    # QR Code with subtle border/glow effect
    qr_img = generate_qr_code(UNIVERSAL_SETUP_URL, qr_size)
    if qr_img:
        qr_x = center_x - qr_size // 2
        qr_y = y

        # Draw subtle orange border around QR code
        border_padding = 8
        border_color = JAM_ORANGE_PRIMARY
        draw.rectangle(
            [qr_x - border_padding, qr_y - border_padding,
             qr_x + qr_size + border_padding, qr_y + qr_size + border_padding],
            outline=border_color,
            width=3
        )

        img.paste(qr_img, (qr_x, qr_y))
        y += qr_size + 50

    # "Get ready to JAM." tagline
    tagline = "Get ready to JAM."
    draw.text(
        (center_x, y),
        tagline,
        font=tagline_font,
        fill=JAM_ORANGE_SECONDARY,
        anchor="mt"
    )

    # Device UUID at bottom (small, subtle)
    if device_uuid:
        device_text = f"Device: {device_uuid}"
        draw.text(
            (center_x, height - 50),
            device_text,
            font=device_font,
            fill=SECONDARY_COLOR,
            anchor="mm"
        )

    # Version indicator in bottom-right corner
    version_font = get_font(14, bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),  # Very subtle
        anchor="mm"
    )

    return img


def create_waiting_for_content_screen(width: int, height: int, screen_id: str = None) -> Image.Image:
    """
    Create the screen for LINKED_WAITING_FOR_CONTENT mode.

    Modern gradient design showing content download progress message.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    # Create cool mesh gradient background for loading state
    img = create_mesh_gradient_background(width, height, theme="cool")
    draw = ImageDraw.Draw(img)

    # Fonts
    title_font = get_font(FONT_SIZE_TITLE)
    subtitle_font = get_font(FONT_SIZE_SUBTITLE, bold=False)
    screen_font = get_font(FONT_SIZE_DEVICE_ID, bold=False)

    center_x = width // 2
    center_y = height // 2

    # Logo at top
    logo_height = min(80, height // 10)
    logo = load_and_scale_logo(logo_height)
    if logo:
        logo_x = center_x - logo.width // 2
        logo_y = int(height * 0.15)
        img.paste(logo, (logo_x, logo_y), logo if logo.mode == 'RGBA' else None)

    # Title - centered
    title = "Waiting for content..."
    draw.text(
        (center_x, center_y - 40),
        title,
        font=title_font,
        fill=JAM_ORANGE_PRIMARY,
        anchor="mm"
    )

    # Subtitle
    subtitle = "Content is being downloaded. This may take a few minutes."
    draw.text(
        (center_x, center_y + 40),
        subtitle,
        font=subtitle_font,
        fill=TEXT_COLOR,
        anchor="mm"
    )

    # Animated-looking dots (static, but gives impression of activity)
    # Draw three dots with varying opacity to suggest animation
    dot_y = center_y + 100
    dot_spacing = 30
    dot_radius = 8
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

    # Screen ID at bottom if available
    if screen_id:
        screen_text = f"Screen: {screen_id}"
        draw.text(
            (center_x, height - 50),
            screen_text,
            font=screen_font,
            fill=SECONDARY_COLOR,
            anchor="mm"
        )

    # Version indicator
    version_font = get_font(14, bold=False)
    draw.text(
        (width - 30, height - 25),
        "v2",
        font=version_font,
        fill=(80, 80, 80),
        anchor="mm"
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
            '--no-audio',
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

    def seek(self, position_seconds: float) -> bool:
        """Seek to a position in seconds (absolute)."""
        return self._send_command(['seek', str(position_seconds), 'absolute']) is not None

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

        # MPV crash tracking for self-healing
        # If MPV crashes too many times in a short period, restart lightdm
        self._mpv_crash_times: list = []
        self._mpv_crash_threshold = 5  # Number of crashes
        self._mpv_crash_window_seconds = 30  # Time window to track crashes

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
        """Determine which display mode we should be in based on current state."""

        # Check registration status - if not registered OR not linked to a screen,
        # show the setup/unregistered screen
        if not is_device_registered():
            return DisplayMode.UNREGISTERED

        # Check if linked to a screen - if not linked, still show setup screen
        screen_id = get_screen_id()
        if not screen_id:
            return DisplayMode.UNREGISTERED

        # Check if content is available
        if not self._has_content():
            return DisplayMode.LINKED_WAITING_FOR_CONTENT

        return DisplayMode.PLAYING_CONTENT

    def _has_content(self) -> bool:
        """
        Check if we have content to display.

        Returns True only if:
        1. scenes.json exists
        2. At least one scene has a media file that exists
        """
        scenes_file = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "scenes.json"
        if not scenes_file.exists():
            return False

        try:
            with open(scenes_file, 'r') as f:
                scenes = json.load(f)

            if not scenes:
                return False

            # Check if at least one media file exists
            media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
            for scene in scenes:
                media_file = scene.get('media_file')
                if media_file and (media_dir / media_file).exists():
                    return True

            # No valid media files found
            return False

        except Exception as e:
            logger.warning(f"Error checking content: {e}")
            return False

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

        if new_mode == DisplayMode.UNREGISTERED:
            logger.info("Showing UNREGISTERED screen (setup/QR code)")
            img = create_unregistered_screen(
                self.screen_width, self.screen_height, device_uuid
            )
            self.feh_process = display_image_with_feh(
                img, "jam_display_unregistered",
                fallback_message="JAM Player\n\nSet up with JAM Player Setup App\nScan QR code to begin"
            )
            if self.feh_process:
                logger.info(f"feh process started: PID {self.feh_process.pid}")
            else:
                logger.error("Failed to start feh for UNREGISTERED screen")
            sd_notifier.notify("STATUS=Showing setup screen")

        elif new_mode == DisplayMode.LINKED_WAITING_FOR_CONTENT:
            logger.info("Showing LINKED_WAITING_FOR_CONTENT screen")
            screen_id = get_screen_id()
            img = create_waiting_for_content_screen(
                self.screen_width, self.screen_height, screen_id
            )
            self.feh_process = display_image_with_feh(
                img, "jam_display_waiting",
                fallback_message="Waiting for content...\n\nContent is being downloaded.\nThis may take a few minutes."
            )
            if self.feh_process:
                logger.info(f"feh process started: PID {self.feh_process.pid}")
            else:
                logger.error("Failed to start feh for LINKED_WAITING_FOR_CONTENT screen")
            sd_notifier.notify("STATUS=Waiting for content download")

        elif new_mode == DisplayMode.PLAYING_CONTENT:
            logger.info("Entering PLAYING_CONTENT mode")
            self._start_video_playback()
            sd_notifier.notify("STATUS=Playing content")

    def _start_video_playback(self):
        """Initialize and start video playback."""
        # Kill any feh processes first
        kill_feh_processes()

        # Initialize MPV
        self.mpv = MpvIpcClient()

        # Get rotation from device orientation setting
        rotation = get_rotation_angle()

        # Get first scene file - MPV must start with a file (idle mode doesn't work)
        scenes = self._load_scenes()
        if not scenes:
            logger.error("No scenes available for playback")
            return

        media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
        first_scene = scenes[0]
        media_file = first_scene.get('media_file')
        if not media_file:
            logger.error("First scene has no media_file")
            return

        initial_file = str(media_dir / media_file)
        if not Path(initial_file).exists():
            logger.error(f"First scene media file not found: {initial_file}")
            return

        # Start MPV with the first file (required - idle mode doesn't display)
        # Enable looping for single-scene content to avoid freeze at end
        single_scene = len(scenes) == 1 if scenes else False
        if not self.mpv.start_mpv(rotation_angle=rotation, loop=single_scene, initial_file=initial_file):
            logger.error("Failed to start MPV")
            return

        logger.info(f"MPV started with initial file: {initial_file}")
        self.is_playing = True

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
            self._start_video_playback()
            if not self.mpv:
                logger.error("Failed to start MPV")
                return

        logger.info("=" * 60)
        logger.info("Starting SYNCED content playback (wall clock mode)")
        logger.info(f"Sync config: check={SYNC_CHECK_INTERVAL_MS}ms, tolerance={TARGET_SYNC_TOLERANCE_MS}ms")
        logger.info("=" * 60)

        self._run_scene_by_scene_sync()

    def _show_no_scheduled_content_screen(self):
        """Show a message when content exists but all scenes are scheduled off."""
        logger.info("All scenes scheduled off - showing 'no content scheduled' message")

        # Stop MPV if running
        if self.mpv:
            self.mpv.stop_mpv()
            self.mpv = None

        # Create and show a message with gradient background
        if HAS_PIL:
            # Warm mesh gradient for "off hours" theme
            img = create_mesh_gradient_background(
                self.screen_width, self.screen_height, theme="warm"
            )
            draw = ImageDraw.Draw(img)

            title_font = get_font(FONT_SIZE_TITLE)
            subtitle_font = get_font(FONT_SIZE_SUBTITLE, bold=False)

            center_x = self.screen_width // 2
            center_y = self.screen_height // 2

            # Logo at top
            logo_height = min(80, self.screen_height // 10)
            logo = load_and_scale_logo(logo_height)
            if logo:
                logo_x = center_x - logo.width // 2
                logo_y = int(self.screen_height * 0.15)
                img.paste(logo, (logo_x, logo_y), logo if logo.mode == 'RGBA' else None)

            # Title
            title = "No Content Scheduled"
            draw.text(
                (center_x, center_y - 30),
                title,
                font=title_font,
                fill=JAM_ORANGE_PRIMARY,
                anchor="mm"
            )

            # Subtitle
            subtitle = "Content will appear during scheduled hours."
            draw.text(
                (center_x, center_y + 40),
                subtitle,
                font=subtitle_font,
                fill=TEXT_COLOR,
                anchor="mm"
            )

            # Version indicator
            version_font = get_font(14, bold=False)
            draw.text(
                (self.screen_width - 30, self.screen_height - 25),
                "v2",
                font=version_font,
                fill=(80, 80, 80),
                anchor="mm"
            )
        else:
            img = None

        self.feh_process = display_image_with_feh(
            img, "jam_display_no_schedule",
            fallback_message="No Content Scheduled\n\nContent will appear\nduring scheduled hours."
        )

    def _run_scene_by_scene_sync(self):
        """
        Play scenes one by one with wall clock sync.
        Supports dynamic scheduling - periodically re-filters scenes by day/time.
        """
        media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)
        scenes = self._load_scenes()

        if not scenes:
            logger.warning("No scenes loaded (all may be scheduled off)")
            # Show "no scheduled content" screen instead of black
            self._show_no_scheduled_content_screen()
            # Wait for schedule to potentially change
            while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
                time.sleep(10)
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
                    # All scenes now scheduled off - show message screen
                    logger.info("All scenes now scheduled off - showing waiting screen")
                    self._show_no_scheduled_content_screen()
                    scenes = []
                    self._current_scene_index = -1

            if not scenes:
                # Check if this is "no content at all" vs "content exists but scheduled off"
                # If no content exists, exit so mode can be re-evaluated
                if not self._has_content():
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

            # Check if MPV died and needs restart
            if self.mpv and not self.mpv.is_running():
                logger.warning("MPV process died, restarting...")

                # Track this crash for self-healing detection
                current_time = time.time()
                self._mpv_crash_times.append(current_time)

                # Remove old crash times outside the window
                self._mpv_crash_times = [
                    t for t in self._mpv_crash_times
                    if current_time - t < self._mpv_crash_window_seconds
                ]

                # Check if we've hit the crash threshold - indicates display subsystem issue
                if len(self._mpv_crash_times) >= self._mpv_crash_threshold:
                    logger.error(
                        f"MPV crashed {len(self._mpv_crash_times)} times in "
                        f"{self._mpv_crash_window_seconds} seconds - restarting lightdm to fix display"
                    )
                    self._mpv_crash_times.clear()  # Reset counter
                    try:
                        # Restart lightdm to reinitialize Xwayland
                        subprocess.run(
                            ['systemctl', 'restart', 'lightdm'],
                            timeout=30,
                            capture_output=True
                        )
                        logger.info("lightdm restart triggered, waiting for display to reinitialize...")
                        time.sleep(5)  # Give lightdm time to restart
                    except Exception as e:
                        logger.error(f"Failed to restart lightdm: {e}")

                # Clean up the dead MPV first
                try:
                    self.mpv.stop_mpv()
                except Exception as e:
                    logger.warning(f"Error stopping dead MPV: {e}")
                self.mpv = None
                # Restart video playback
                self._start_video_playback()
                self._current_scene_index = -1  # Force scene reload
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
                    logger.error(f"Media file not found: {media_path}")
                    time.sleep(0.5)
                    continue

                logger.debug(f"Switching to scene {scene_index}: {scene.get('id')} ({media_type})")
                self._current_scene_index = scene_index

                # Just load and play - no seeking or sync logic for now
                self.mpv.load_file(str(media_path))
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

    def run(self):
        """Main run loop - monitors state and manages display modes."""
        log_service_start(logger, 'JAM Player Display Service')

        logger.info("=" * 60)
        logger.info("JAM PLAYER DISPLAY SERVICE - 4-MODE UNIFIED DISPLAY")
        logger.info(f"Screen: {self.screen_width}x{self.screen_height}")
        _log_dependency_status()
        logger.info("=" * 60)

        # Send READY=1 immediately - we're initialized and entering main loop
        # Display availability is handled within the loop, not a startup blocker
        sd_notifier.notify("READY=1")
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
                        self.transition_to_mode(new_mode)

                # If in playing mode, run the video loop (blocking until state changes)
                if self.current_mode == DisplayMode.PLAYING_CONTENT:
                    self.run_video_loop()
                    # After video loop exits, recheck state
                    continue

                # For static display modes, check feh is still running and sleep
                if self.feh_process:
                    poll_result = self.feh_process.poll()
                    if poll_result is not None:
                        # feh has exited - this shouldn't happen
                        logger.warning(f"feh process exited with code {poll_result}, restarting display")
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
