#!/usr/bin/env python3
"""
JAM Player Display Service - Unified Display Manager

This service handles all display states for the JAM Player:

1. UNREGISTERED (.registered doesn't exist)
   - Display: Welcome screen with QR code to JAM Setup app
   - Shows JAM-PLAYER-XXXXX identifier for BLE pairing

2. REGISTERED_NOT_LINKED (.registered exists, screen_id.txt doesn't exist)
   - Display: "Registered! Open JAM Setup to link this player to a screen"
   - Shows same QR code for the app

3. LINKED_WAITING_FOR_CONTENT (screen_id.txt exists, no content downloaded)
   - Display: "Waiting for content..." message

4. PLAYING_CONTENT (screen_id.txt exists, content is available)
   - Display: Plays scenes sequentially from scenes.json
   - Videos play fully, images display for their configured duration
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
    """The 4 display modes from the design doc."""
    UNREGISTERED = "unregistered"
    REGISTERED_NOT_LINKED = "registered_not_linked"
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

# Display configuration
BACKGROUND_COLOR = (0, 0, 0)  # Black
TEXT_COLOR = (255, 255, 255)  # White
ACCENT_COLOR = (235, 68, 15)  # JAM orange #eb440f
SECONDARY_COLOR = (180, 180, 180)  # Grey for less prominent text

FONT_SIZE_TITLE = 72
FONT_SIZE_SUBTITLE = 36
FONT_SIZE_INSTRUCTIONS = 32
FONT_SIZE_URL = 28
FONT_SIZE_DEVICE_ID = 24

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


def get_font(size: int):
    """Get a font, falling back to default if needed."""
    if not HAS_PIL:
        return None

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


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
    Create the welcome/setup screen for UNREGISTERED mode.
    Shows QR code and instructions for using JAM Setup app.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = get_font(FONT_SIZE_TITLE)
    subtitle_font = get_font(FONT_SIZE_SUBTITLE)
    instructions_font = get_font(FONT_SIZE_INSTRUCTIONS)
    url_font = get_font(FONT_SIZE_URL)

    center_x = width // 2
    qr_size = min(400, height // 3)
    y = height // 8

    # Title
    title = "Welcome to JAM Player"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    x = center_x - bbox[2] // 2
    draw.text((x, y), title, font=title_font, fill=ACCENT_COLOR)
    y += bbox[3] + 40

    # Subtitle
    subtitle = "Let's get your device set up"
    bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    x = center_x - bbox[2] // 2
    draw.text((x, y), subtitle, font=subtitle_font, fill=TEXT_COLOR)
    y += bbox[3] + 60

    # BLE device name (if we have the UUID)
    device_short = get_device_uuid_short()
    if device_short:
        ble_name = f"JAM-PLAYER-{device_short}"
        ble_text = f"Select this device in the app: {ble_name}"
        bbox = draw.textbbox((0, 0), ble_text, font=instructions_font)
        x = center_x - bbox[2] // 2
        draw.text((x, y), ble_text, font=instructions_font, fill=ACCENT_COLOR)
        y += bbox[3] + 40

    # QR Code
    qr_img = generate_qr_code(UNIVERSAL_SETUP_URL, qr_size)
    if qr_img:
        qr_x = center_x - qr_size // 2
        qr_y = y
        img.paste(qr_img, (qr_x, qr_y))
        y += qr_size + 40

    # Instructions
    instructions = [
        "1. Scan the QR code with your phone",
        "2. Download the JAM Setup app",
        "3. Follow the in-app instructions",
    ]

    for instruction in instructions:
        bbox = draw.textbbox((0, 0), instruction, font=instructions_font)
        x = center_x - bbox[2] // 2
        draw.text((x, y), instruction, font=instructions_font, fill=TEXT_COLOR)
        y += bbox[3] + 20

    # Bottom elements - using anchor="ms" (middle-baseline) for easy positioning
    # Layout from bottom: Device UUID at bottom, URL 60px above it

    # URL - draw right after instructions (continues from current y position)
    y += 40  # spacing after instructions
    url_text = f"Or visit: {UNIVERSAL_SETUP_URL}"
    draw.text(
        (center_x, y),
        url_text,
        font=url_font,
        fill=SECONDARY_COLOR,
        anchor="mm"
    )

    # Device UUID at fixed position at bottom
    if device_uuid:
        device_text = f"Device: {device_uuid}"
        device_font = get_font(FONT_SIZE_DEVICE_ID)
        draw.text(
            (center_x, height - 80),
            device_text,
            font=device_font,
            fill=SECONDARY_COLOR,
            anchor="mm"
        )

    # Version indicator in bottom-right corner (temporary - to verify deployment)
    version_font = get_font(16)
    draw.text((width - 50, height - 30), "v2", font=version_font, fill=SECONDARY_COLOR)

    return img


def create_registered_not_linked_screen(width: int, height: int, device_uuid: str = None) -> Image.Image:
    """
    Create the screen for REGISTERED_NOT_LINKED mode.
    Shows "Registered! Link to a screen" message with QR code.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = get_font(FONT_SIZE_TITLE)
    subtitle_font = get_font(FONT_SIZE_SUBTITLE)
    instructions_font = get_font(FONT_SIZE_INSTRUCTIONS)
    url_font = get_font(FONT_SIZE_URL)

    center_x = width // 2
    qr_size = min(350, height // 4)
    y = height // 8

    # Title - success message
    title = "Registered!"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    x = center_x - bbox[2] // 2
    draw.text((x, y), title, font=title_font, fill=ACCENT_COLOR)
    y += bbox[3] + 40

    # Subtitle
    subtitle = "Open JAM Setup to link this player to a screen"
    bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    x = center_x - bbox[2] // 2
    draw.text((x, y), subtitle, font=subtitle_font, fill=TEXT_COLOR)
    y += bbox[3] + 60

    # QR Code
    qr_img = generate_qr_code(UNIVERSAL_SETUP_URL, qr_size)
    if qr_img:
        qr_x = center_x - qr_size // 2
        qr_y = y
        img.paste(qr_img, (qr_x, qr_y))
        y += qr_size + 40

    # Instructions
    instructions = [
        "1. Open the JAM Setup app",
        "2. Go to 'My JAM Players'",
        "3. Select this device and link it to a screen",
    ]

    # Calculate device UUID position first (fixed at bottom)
    device_uuid_y = height - 70  # Reserve space at bottom

    for instruction in instructions:
        bbox = draw.textbbox((0, 0), instruction, font=instructions_font)
        # Only draw if it won't overlap with device UUID area
        if y + bbox[3] < device_uuid_y - 20:
            x = center_x - bbox[2] // 2
            draw.text((x, y), instruction, font=instructions_font, fill=TEXT_COLOR)
            y += bbox[3] + 20

    # Device UUID at bottom (fixed position)
    if device_uuid:
        device_text = f"Device: {device_uuid}"
        device_font = get_font(FONT_SIZE_DEVICE_ID)
        bbox = draw.textbbox((0, 0), device_text, font=device_font)
        x = center_x - bbox[2] // 2
        draw.text((x, device_uuid_y), device_text, font=device_font, fill=SECONDARY_COLOR)

    return img


def create_waiting_for_content_screen(width: int, height: int, screen_id: str = None) -> Image.Image:
    """
    Create the screen for LINKED_WAITING_FOR_CONTENT mode.
    Shows "Waiting for content" message.
    """
    if not HAS_PIL:
        logger.error("PIL not available for creating display images")
        return None

    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = get_font(FONT_SIZE_TITLE)
    subtitle_font = get_font(FONT_SIZE_SUBTITLE)

    center_x = width // 2
    center_y = height // 2

    # Title
    title = "Waiting for content..."
    bbox = draw.textbbox((0, 0), title, font=title_font)
    x = center_x - bbox[2] // 2
    y = center_y - bbox[3] - 20
    draw.text((x, y), title, font=title_font, fill=ACCENT_COLOR)

    # Subtitle
    subtitle = "Content is being downloaded. This may take a few minutes."
    bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    x = center_x - bbox[2] // 2
    y = center_y + 20
    draw.text((x, y), subtitle, font=subtitle_font, fill=TEXT_COLOR)

    # Screen ID at bottom if available
    if screen_id:
        screen_text = f"Screen ID: {screen_id}"
        screen_font = get_font(FONT_SIZE_DEVICE_ID)
        bbox = draw.textbbox((0, 0), screen_text, font=screen_font)
        x = center_x - bbox[2] // 2
        y = height - bbox[3] - 30
        draw.text((x, y), screen_text, font=screen_font, fill=SECONDARY_COLOR)

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

    def start_mpv(self, rotation_angle: int = 0, loop: bool = True) -> bool:
        """Start MPV process with IPC socket enabled.

        Args:
            rotation_angle: Video rotation in degrees
            loop: If True, loop videos infinitely (legacy mode). If False, play once (scene mode).
        """
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.stop_mpv()

        args = [
            'mpv',
            '--idle=yes',
            '--fullscreen',
            '--no-osc',
            '--no-osd-bar',
            '--no-input-default-bindings',
            '--input-conf=/dev/null',
            '--force-window=yes',
            '--no-terminal',
            '--keep-open=yes',
            '--no-audio',  # Silent playback - no audio output
            '--hwdec=auto',  # Use hardware decoding when available (critical for Pi)
            '--image-display-duration=inf',  # Don't auto-advance images
            '--hr-seek=yes',
            '--cache=yes',
            '--demuxer-max-bytes=150M',
            '--demuxer-readahead-secs=20',
            '--video-sync=audio',  # Sync to audio clock (even with --no-audio, this is more stable)
            f'--video-rotate={rotation_angle}',
            f'--input-ipc-server={self.socket_path}',
        ]

        # Add loop option only for legacy single-video mode
        if loop:
            args.insert(-1, '--loop-file=inf')
            args.insert(-1, '--hr-seek-framedrop=no')

        try:
            logger.info(f"Starting MPV with IPC socket at {self.socket_path}")

            # Set DISPLAY environment for MPV
            env = os.environ.copy()
            env['DISPLAY'] = ':0'
            env['XAUTHORITY'] = '/home/comitup/.Xauthority'

            self.process = subprocess.Popen(
                args,
                env=env,
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

        # Check registration status
        if not is_device_registered():
            return DisplayMode.UNREGISTERED

        # Check if linked to a screen
        screen_id = get_screen_id()
        if not screen_id:
            return DisplayMode.REGISTERED_NOT_LINKED

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
            logger.info("Showing UNREGISTERED screen (welcome/QR code)")
            img = create_unregistered_screen(
                self.screen_width, self.screen_height, device_uuid
            )
            self.feh_process = display_image_with_feh(
                img, "jam_display_unregistered",
                fallback_message="Welcome to JAM Player\n\nDownload JAM Setup app\nto configure this device"
            )
            if self.feh_process:
                logger.info(f"feh process started: PID {self.feh_process.pid}")
            else:
                logger.error("Failed to start feh for UNREGISTERED screen")
            sd_notifier.notify("STATUS=Showing setup screen")

        elif new_mode == DisplayMode.REGISTERED_NOT_LINKED:
            logger.info("Showing REGISTERED_NOT_LINKED screen")
            img = create_registered_not_linked_screen(
                self.screen_width, self.screen_height, device_uuid
            )
            self.feh_process = display_image_with_feh(
                img, "jam_display_not_linked",
                fallback_message="Registered!\n\nOpen JAM Setup app\nto link this player to a screen"
            )
            if self.feh_process:
                logger.info(f"feh process started: PID {self.feh_process.pid}")
            else:
                logger.error("Failed to start feh for REGISTERED_NOT_LINKED screen")
            sd_notifier.notify("STATUS=Registered - waiting for screen link")

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

        # TODO: Get rotation from device configuration
        rotation = 0

        # Start MPV without looping - we handle scene transitions manually
        if not self.mpv.start_mpv(rotation_angle=rotation, loop=False):
            logger.error("Failed to start MPV")
            return

        logger.info("MPV started successfully")
        self.is_playing = False  # Will be set true once we load a file

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

    def _load_loop_metadata(self) -> Optional[dict]:
        """Load loop_meta.json if available."""
        meta_path = Path(constants.APP_DATA_LIVE_SCENES_DIR) / "loop_meta.json"
        if meta_path.exists():
            try:
                with open(meta_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load loop metadata: {e}")
        return None

    def _has_scheduled_scenes(self) -> bool:
        """Check if any scene has scheduling constraints."""
        scenes = self._load_scenes(apply_schedule_filter=False)
        for scene in scenes:
            days_scheduled = scene.get('days_scheduled', [])
            if days_scheduled:
                return True
        return False

    def run_video_loop(self):
        """
        Main content playback loop with wall clock synchronization.

        If scenes have scheduling constraints (days_scheduled), uses scene-by-scene
        playback so we can dynamically filter based on current day/time.

        Otherwise uses the stitched loop.mp4 if available (preferred - gapless playback).

        All JAM Players displaying the same Screen will show the same content
        at the same time, synchronized via wall clock (chrony/NTP).
        """
        if not self.mpv:
            return

        logger.info("=" * 60)
        logger.info("Starting SYNCED content playback (wall clock mode)")
        logger.info(f"Sync config: check={SYNC_CHECK_INTERVAL_MS}ms, tolerance={TARGET_SYNC_TOLERANCE_MS}ms")
        logger.info("=" * 60)

        media_dir = Path(constants.APP_DATA_LIVE_MEDIA_DIR)

        # Check if any scene has scheduling - if so, use scene-by-scene for dynamic filtering
        if self._has_scheduled_scenes():
            logger.info("Scenes have scheduling constraints - using scene-by-scene playback for dynamic filtering")
            self._run_scene_by_scene_sync()
            return

        # Check for stitched loop.mp4 (preferred for gapless playback)
        loop_path = media_dir / "loop.mp4"
        loop_meta = self._load_loop_metadata()

        if loop_path.exists() and loop_meta:
            self._run_loop_video_sync(loop_path, loop_meta)
        else:
            logger.warning("No stitched loop.mp4 found - using scene-by-scene playback (may have transition glitches)")
            self._run_scene_by_scene_sync()

    def _run_loop_video_sync(self, loop_path: Path, loop_meta: dict):
        """
        Play the stitched loop.mp4 with wall clock synchronization.
        This is the preferred mode - gapless playback with tight sync.
        """
        loop_duration_sec = loop_meta.get('total_duration', 60)
        loop_duration_ms = int(loop_duration_sec * 1000)
        scene_count = loop_meta.get('scene_count', 0)

        logger.info(f"Playing stitched loop: {loop_path}")
        logger.info(f"Loop duration: {loop_duration_sec:.1f}s ({scene_count} scenes)")

        # Load the loop video
        self.mpv.load_file(str(loop_path))
        time.sleep(0.5)

        # Initial sync - seek to correct position
        expected_ms = self._calculate_expected_position(loop_duration_ms)
        expected_sec = expected_ms / 1000.0
        self.mpv.seek(expected_sec)
        self.mpv.set_property('pause', False)
        self.mpv.set_speed(SPEED_NORMAL)
        self._current_speed = SPEED_NORMAL

        logger.info(f"Initial sync: seeking to {expected_sec:.2f}s")

        self._last_sync_check = 0
        self._last_sync_log = 0
        self._last_content_mtime = 0
        sync_stats = {'adjustments': 0, 'seeks': 0, 'in_sync': 0}

        while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
            # Check for content updates (loop.mp4 rebuilt)
            try:
                current_mtime = loop_path.stat().st_mtime
                if self._last_content_mtime == 0:
                    self._last_content_mtime = current_mtime
                elif current_mtime != self._last_content_mtime:
                    logger.info("Loop video updated - reloading")
                    self._last_content_mtime = current_mtime
                    # Reload loop metadata
                    new_meta = self._load_loop_metadata()
                    if new_meta:
                        loop_duration_sec = new_meta.get('total_duration', 60)
                        loop_duration_ms = int(loop_duration_sec * 1000)
                    # Reload and sync
                    self.mpv.load_file(str(loop_path))
                    time.sleep(0.3)
                    expected_ms = self._calculate_expected_position(loop_duration_ms)
                    self.mpv.seek(expected_ms / 1000.0)
                    self.mpv.set_speed(SPEED_NORMAL)
                    self._current_speed = SPEED_NORMAL
                    continue
            except Exception as e:
                logger.warning(f"Error checking loop file: {e}")

            # Sync adjustment
            current_time_ms = self._get_wall_clock_ms()

            if current_time_ms - self._last_sync_check >= SYNC_CHECK_INTERVAL_MS:
                self._last_sync_check = current_time_ms

                # Get actual playback position
                actual_sec = self.mpv.get_playback_time()
                if actual_sec is not None:
                    actual_ms = int(actual_sec * 1000)
                    expected_ms = self._calculate_expected_position(loop_duration_ms)
                    offset_ms = self._get_sync_offset_ms(expected_ms, actual_ms, loop_duration_ms)
                    abs_offset = abs(offset_ms)

                    # Apply proportional speed control
                    if abs_offset > SEEK_THRESHOLD_MS:
                        # Emergency seek
                        target_sec = expected_ms / 1000.0
                        logger.warning(f"SYNC EMERGENCY SEEK: offset={offset_ms}ms -> {target_sec:.2f}s")
                        self.mpv.seek(target_sec)
                        self.mpv.set_speed(SPEED_NORMAL)
                        self._current_speed = SPEED_NORMAL
                        sync_stats['seeks'] += 1

                    elif abs_offset > 100:
                        new_speed = SPEED_AGGRESSIVE_FAST if offset_ms < 0 else SPEED_AGGRESSIVE_SLOW
                        if new_speed != self._current_speed:
                            self.mpv.set_speed(new_speed)
                            self._current_speed = new_speed
                            sync_stats['adjustments'] += 1

                    elif abs_offset > 30:
                        new_speed = SPEED_MODERATE_FAST if offset_ms < 0 else SPEED_MODERATE_SLOW
                        if new_speed != self._current_speed:
                            self.mpv.set_speed(new_speed)
                            self._current_speed = new_speed
                            sync_stats['adjustments'] += 1

                    elif abs_offset > TARGET_SYNC_TOLERANCE_MS:
                        new_speed = SPEED_GENTLE_FAST if offset_ms < 0 else SPEED_GENTLE_SLOW
                        if new_speed != self._current_speed:
                            self.mpv.set_speed(new_speed)
                            self._current_speed = new_speed
                            sync_stats['adjustments'] += 1

                    else:
                        if self._current_speed != SPEED_NORMAL:
                            self.mpv.set_speed(SPEED_NORMAL)
                            self._current_speed = SPEED_NORMAL
                        sync_stats['in_sync'] += 1

                    # Log sync status every 5 seconds
                    if current_time_ms - self._last_sync_log >= 5000:
                        self._last_sync_log = current_time_ms
                        status = "IN_SYNC" if abs_offset <= TARGET_SYNC_TOLERANCE_MS else "ADJUSTING"
                        speed_str = f"{self._current_speed:.2f}x"
                        logger.info(
                            f"SYNC [{status}]: offset={offset_ms:+d}ms speed={speed_str} "
                            f"| pos={actual_sec:.1f}s/{loop_duration_sec:.1f}s "
                            f"| stats={{seeks:{sync_stats['seeks']}, adj:{sync_stats['adjustments']}, sync:{sync_stats['in_sync']}}}"
                        )

            time.sleep(0.05)

    def _show_no_scheduled_content_screen(self):
        """Show a message when content exists but all scenes are scheduled off."""
        logger.info("All scenes scheduled off - showing 'no content scheduled' message")

        # Stop MPV if running
        if self.mpv:
            self.mpv.stop_mpv()
            self.mpv = None

        # Create and show a message
        if HAS_PIL:
            img = Image.new('RGB', (self.screen_width, self.screen_height), BACKGROUND_COLOR)
            draw = ImageDraw.Draw(img)
            title_font = get_font(FONT_SIZE_TITLE)
            subtitle_font = get_font(FONT_SIZE_SUBTITLE)

            center_x = self.screen_width // 2
            center_y = self.screen_height // 2

            title = "No Content Scheduled"
            bbox = draw.textbbox((0, 0), title, font=title_font)
            x = center_x - bbox[2] // 2
            y = center_y - bbox[3] - 20
            draw.text((x, y), title, font=title_font, fill=ACCENT_COLOR)

            subtitle = "Content will appear during scheduled hours."
            bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
            x = center_x - bbox[2] // 2
            y = center_y + 20
            draw.text((x, y), subtitle, font=subtitle_font, fill=TEXT_COLOR)
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

                    # Check if active scene list changed
                    old_scene_ids = [s.get('id') for s in scenes]
                    new_scene_ids = [s.get('id') for s in new_scenes]

                    if old_scene_ids != new_scene_ids:
                        scenes = new_scenes
                        cycle_duration_ms = self._calculate_cycle_duration_ms(scenes)
                        self._current_scene_index = -1
                        logger.info(f"Active scenes changed: {len(scenes)} scenes, cycle: {cycle_duration_ms}ms")
                elif not new_scenes and scenes:
                    # All scenes now scheduled off - show message screen
                    logger.info("All scenes now scheduled off - showing waiting screen")
                    self._show_no_scheduled_content_screen()
                    scenes = []
                    self._current_scene_index = -1

            if not scenes:
                # Wait and re-check for scheduled scenes
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

            # Calculate where we should be based on wall clock
            position_in_cycle_ms = self._calculate_expected_position(cycle_duration_ms)
            scene_index, position_in_scene_ms, scene = self._get_scene_at_position(scenes, position_in_cycle_ms)

            media_file = scene.get('media_file')
            media_type = scene.get('media_type', 'VIDEO')  # All content is video now
            scene_duration_ms = int(scene.get('actual_duration', scene.get('duration', 15)) * 1000)
            media_path = media_dir / media_file

            # Check if we need to switch scenes
            if scene_index != self._current_scene_index:
                if not media_path.exists():
                    logger.error(f"Media file not found: {media_path}")
                    time.sleep(0.5)
                    continue

                logger.info(f"Switching to scene {scene_index}: {scene.get('id')} ({media_type}) @ {position_in_scene_ms}ms")
                self._current_scene_index = scene_index

                self.mpv.load_file(str(media_path))
                time.sleep(0.2)

                if media_type == 'VIDEO':
                    seek_sec = position_in_scene_ms / 1000.0
                    self.mpv.seek(seek_sec)
                    self.mpv.set_speed(SPEED_NORMAL)
                    self._current_speed = SPEED_NORMAL

            # Sync logic for videos
            current_time_ms = self._get_wall_clock_ms()

            if current_time_ms - self._last_sync_check >= SYNC_CHECK_INTERVAL_MS:
                self._last_sync_check = current_time_ms

                if media_type == 'VIDEO':
                    self._adjust_video_sync(scene_duration_ms, position_in_scene_ms)

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

        logger.info(f"Video duration: {duration:.1f}s")

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
        logger.info(f"Displaying image for {duration_seconds}s")

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
