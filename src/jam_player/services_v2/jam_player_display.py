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
   - Display: The main stitched content video on infinite loop
   - Uses wall-clock synchronization for multi-screen sync (<50ms drift)

This service monitors state changes and transitions between display modes automatically.
It should be ultra-stable - the PLAYING_CONTENT mode remains "dumb" and just loops video.
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
from typing import Optional, Any
from dataclasses import dataclass

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
    CONTENT_DIR,
    MEDIA_DIR,
    LOOP_VIDEO_PATH,
    LEGACY_MEDIA_DIR,
    LEGACY_LOOP_VIDEO_PATH,
)

# Try to import PIL for setup screens
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Try to import qrcode for setup screens
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

logger = setup_service_logging('jam-player-display')
sd_notifier = get_systemd_notifier()


class DisplayMode(Enum):
    """The 4 display modes from the design doc."""
    UNREGISTERED = "unregistered"
    REGISTERED_NOT_LINKED = "registered_not_linked"
    LINKED_WAITING_FOR_CONTENT = "linked_waiting_for_content"
    PLAYING_CONTENT = "playing_content"


# =============================================================================
# Configuration Constants
# =============================================================================

# Note: Content paths (CONTENT_DIR, MEDIA_DIR, LOOP_VIDEO_PATH, LEGACY_*)
# are imported from common.paths

# Display configuration
BACKGROUND_COLOR = (20, 20, 30)  # Dark blue-grey
TEXT_COLOR = (255, 255, 255)
ACCENT_COLOR = (0, 180, 255)  # JAM blue
SECONDARY_COLOR = (180, 180, 180)
WARNING_COLOR = (255, 180, 0)  # Amber for waiting states

FONT_SIZE_TITLE = 72
FONT_SIZE_SUBTITLE = 36
FONT_SIZE_INSTRUCTIONS = 32
FONT_SIZE_URL = 28
FONT_SIZE_DEVICE_ID = 24

# URLs for setup
UNIVERSAL_SETUP_URL = "https://setup.justamenu.com"

# State checking intervals
STATE_CHECK_INTERVAL_SEC = 5

# Sync configuration for video playback
SYNC_CHECK_INTERVAL_MS = 200
SEEK_THRESHOLD_MS = 500
TARGET_SYNC_TOLERANCE_MS = 10

# Speed adjustment tiers
SPEED_NORMAL = 1.0
SPEED_GENTLE_FAST = 1.01
SPEED_GENTLE_SLOW = 0.99
SPEED_MODERATE_FAST = 1.03
SPEED_MODERATE_SLOW = 0.97
SPEED_AGGRESSIVE_FAST = 1.05
SPEED_AGGRESSIVE_SLOW = 0.95


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
    draw.text((x, y), title, font=title_font, fill=(0, 200, 100))  # Green for success
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
    draw.text((x, y), title, font=title_font, fill=WARNING_COLOR)

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


def display_image_with_feh(img: Image.Image, img_name: str = "jam_display") -> Optional[subprocess.Popen]:
    """Display an image fullscreen using feh. Returns the process handle."""
    if img is None:
        return None

    img_path = f'/tmp/{img_name}.png'
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

    def start_mpv(self, rotation_angle: int = 0) -> bool:
        """Start MPV process with IPC socket enabled."""
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
            '--loop-file=inf',
            '--hr-seek=yes',
            '--hr-seek-framedrop=no',
            '--video-sync=audio',
            '--cache=yes',
            '--demuxer-max-bytes=150M',
            '--demuxer-readahead-secs=20',
            f'--video-rotate={rotation_angle}',
            f'--input-ipc-server={self.socket_path}',
        ]

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
        """Load a media file into MPV."""
        self._send_command(['loadfile', filepath, 'replace'])
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

        # Video playback state
        self.loop_duration_ms: int = 0
        self.is_playing: bool = False
        self.current_speed: float = SPEED_NORMAL
        self.last_sync_log_time: float = 0
        self.sync_stats = {'adjustments': 0, 'seeks': 0, 'in_sync_count': 0}

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
        """Check if we have content to display."""
        # Check new JAM 2.0 content path
        if LOOP_VIDEO_PATH.exists():
            return True

        # Check legacy content path
        if LEGACY_LOOP_VIDEO_PATH.exists():
            return True

        # Check for any video in legacy media dir
        if LEGACY_MEDIA_DIR.exists():
            for f in LEGACY_MEDIA_DIR.iterdir():
                if f.suffix.lower() in ('.mp4', '.mkv', '.webm', '.avi'):
                    return True

        return False

    def _get_content_path(self) -> Optional[Path]:
        """Get the path to the content video to play."""
        # Prefer JAM 2.0 path
        if LOOP_VIDEO_PATH.exists():
            return LOOP_VIDEO_PATH

        # Fall back to legacy path
        if LEGACY_LOOP_VIDEO_PATH.exists():
            return LEGACY_LOOP_VIDEO_PATH

        # Check for any video in legacy media dir
        if LEGACY_MEDIA_DIR.exists():
            for f in LEGACY_MEDIA_DIR.iterdir():
                if f.suffix.lower() in ('.mp4', '.mkv', '.webm', '.avi'):
                    return f

        return None

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
            self.feh_process = display_image_with_feh(img, "jam_display_unregistered")
            sd_notifier.notify("STATUS=Showing setup screen")

        elif new_mode == DisplayMode.REGISTERED_NOT_LINKED:
            logger.info("Showing REGISTERED_NOT_LINKED screen")
            img = create_registered_not_linked_screen(
                self.screen_width, self.screen_height, device_uuid
            )
            self.feh_process = display_image_with_feh(img, "jam_display_not_linked")
            sd_notifier.notify("STATUS=Registered - waiting for screen link")

        elif new_mode == DisplayMode.LINKED_WAITING_FOR_CONTENT:
            logger.info("Showing LINKED_WAITING_FOR_CONTENT screen")
            screen_id = get_screen_id()
            img = create_waiting_for_content_screen(
                self.screen_width, self.screen_height, screen_id
            )
            self.feh_process = display_image_with_feh(img, "jam_display_waiting")
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

        if not self.mpv.start_mpv(rotation_angle=rotation):
            logger.error("Failed to start MPV")
            return

        logger.info("MPV started successfully")
        self.is_playing = False  # Will be set true once we load a file

    def _get_wall_clock_ms(self) -> int:
        """Get current wall clock time in milliseconds since epoch."""
        return int(time.time() * 1000)

    def _calculate_expected_position_ms(self, duration_ms: int) -> int:
        """Calculate where in the loop we should be based on wall clock."""
        current_time_ms = self._get_wall_clock_ms()
        return current_time_ms % duration_ms

    def _get_sync_offset_ms(self, duration_ms: int) -> Optional[int]:
        """Calculate offset between actual and expected position."""
        if not self.mpv:
            return None

        expected_ms = self._calculate_expected_position_ms(duration_ms)
        actual_sec = self.mpv.get_playback_time()

        if actual_sec is None:
            return None

        actual_ms = int(actual_sec * 1000)
        offset_ms = actual_ms - expected_ms

        # Handle wrap-around
        if offset_ms > duration_ms / 2:
            offset_ms = offset_ms - duration_ms
        elif offset_ms < -duration_ms / 2:
            offset_ms = offset_ms + duration_ms

        return offset_ms

    def _adjust_sync(self, duration_ms: int):
        """Adjust playback speed to stay in sync with wall clock."""
        offset_ms = self._get_sync_offset_ms(duration_ms)

        if offset_ms is None:
            return

        abs_offset = abs(offset_ms)
        current_time = time.time()

        if abs_offset > SEEK_THRESHOLD_MS:
            # Emergency seek
            expected_ms = self._calculate_expected_position_ms(duration_ms)
            expected_sec = expected_ms / 1000.0
            logger.warning(f"EMERGENCY SEEK: offset={offset_ms}ms, seeking to {expected_sec:.2f}s")
            self.mpv.seek(expected_sec)
            self.mpv.set_speed(SPEED_NORMAL)
            self.current_speed = SPEED_NORMAL
            self.sync_stats['seeks'] += 1

        elif abs_offset > 100:
            new_speed = SPEED_AGGRESSIVE_FAST if offset_ms < 0 else SPEED_AGGRESSIVE_SLOW
            if new_speed != self.current_speed:
                self.mpv.set_speed(new_speed)
                self.current_speed = new_speed
                self.sync_stats['adjustments'] += 1

        elif abs_offset > 30:
            new_speed = SPEED_MODERATE_FAST if offset_ms < 0 else SPEED_MODERATE_SLOW
            if new_speed != self.current_speed:
                self.mpv.set_speed(new_speed)
                self.current_speed = new_speed
                self.sync_stats['adjustments'] += 1

        elif abs_offset > TARGET_SYNC_TOLERANCE_MS:
            new_speed = SPEED_GENTLE_FAST if offset_ms < 0 else SPEED_GENTLE_SLOW
            if new_speed != self.current_speed:
                self.mpv.set_speed(new_speed)
                self.current_speed = new_speed
                self.sync_stats['adjustments'] += 1

        else:
            if self.current_speed != SPEED_NORMAL:
                self.mpv.set_speed(SPEED_NORMAL)
                self.current_speed = SPEED_NORMAL
            self.sync_stats['in_sync_count'] += 1

        # Log periodically
        if current_time - self.last_sync_log_time >= 30.0:
            self.last_sync_log_time = current_time
            status = "IN_SYNC" if abs_offset <= TARGET_SYNC_TOLERANCE_MS else "ADJUSTING"
            logger.info(
                f"SYNC [{status}]: offset={offset_ms:+d}ms speed={self.current_speed:.2f}x "
                f"| stats={{seeks:{self.sync_stats['seeks']}, adj:{self.sync_stats['adjustments']}}}"
            )

    def _initial_sync(self, duration_ms: int):
        """Perform initial synchronization when starting playback."""
        expected_ms = self._calculate_expected_position_ms(duration_ms)
        expected_sec = expected_ms / 1000.0

        logger.info(f"INITIAL SYNC: duration={duration_ms}ms target_position={expected_ms}ms")

        self.mpv.seek(expected_sec)
        self.mpv.set_property('pause', False)
        self.mpv.set_speed(SPEED_NORMAL)
        self.current_speed = SPEED_NORMAL

        time.sleep(0.3)

        offset_ms = self._get_sync_offset_ms(duration_ms)
        if offset_ms is not None:
            logger.info(f"INITIAL SYNC COMPLETE: initial_offset={offset_ms:+d}ms")

    def run_video_loop(self):
        """Main video playback loop - stays here while in PLAYING_CONTENT mode."""
        if not self.mpv:
            return

        content_path = self._get_content_path()
        if not content_path:
            logger.warning("No content available for playback")
            return

        logger.info(f"Loading content: {content_path}")
        self.mpv.load_file(str(content_path))

        # Wait for duration
        duration_sec = None
        for attempt in range(30):
            time.sleep(0.5)
            duration_sec = self.mpv.get_duration()
            if duration_sec is not None and duration_sec > 0:
                break
            if attempt % 4 == 0:
                logger.info(f"Waiting for video duration... attempt {attempt + 1}/30")

        if duration_sec is None or duration_sec <= 0:
            logger.error("Could not get video duration")
            return

        self.loop_duration_ms = int(duration_sec * 1000)
        logger.info(f"Loop duration: {self.loop_duration_ms}ms ({duration_sec:.2f}s)")

        # Initial sync
        self._initial_sync(self.loop_duration_ms)
        self.is_playing = True

        last_sync_check = time.time()
        last_state_check = time.time()

        while self.running and self.current_mode == DisplayMode.PLAYING_CONTENT:
            current_time = time.time()

            # Sync check (every 200ms)
            if (current_time - last_sync_check) * 1000 >= SYNC_CHECK_INTERVAL_MS:
                last_sync_check = current_time
                self._adjust_sync(self.loop_duration_ms)

            # State check (every 5 seconds) - might need to transition
            if current_time - last_state_check >= STATE_CHECK_INTERVAL_SEC:
                last_state_check = current_time
                new_mode = self.determine_display_mode()
                if new_mode != self.current_mode:
                    logger.info(f"State change detected: {self.current_mode} -> {new_mode}")
                    break  # Exit loop to transition

                # Also check if content file changed
                current_content = self._get_content_path()
                if current_content != content_path:
                    logger.info(f"Content changed: {content_path} -> {current_content}")
                    if current_content:
                        # Reload the new content
                        content_path = current_content
                        logger.info(f"Reloading content: {content_path}")
                        self.mpv.load_file(str(content_path))

                        # Re-get duration
                        time.sleep(0.5)
                        for _ in range(10):
                            duration_sec = self.mpv.get_duration()
                            if duration_sec and duration_sec > 0:
                                break
                            time.sleep(0.5)

                        if duration_sec and duration_sec > 0:
                            self.loop_duration_ms = int(duration_sec * 1000)
                            self._initial_sync(self.loop_duration_ms)
                        else:
                            logger.warning("Could not get new content duration")
                    else:
                        # No content anymore
                        break

            # Send watchdog ping
            sd_notifier.notify("WATCHDOG=1")

            time.sleep(0.05)

    def run(self):
        """Main run loop - monitors state and manages display modes."""
        log_service_start(logger, 'JAM Player Display Service')

        logger.info("=" * 60)
        logger.info("JAM PLAYER DISPLAY SERVICE - 4-MODE UNIFIED DISPLAY")
        logger.info(f"Screen: {self.screen_width}x{self.screen_height}")
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

                # For static display modes, just sleep and check state
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
