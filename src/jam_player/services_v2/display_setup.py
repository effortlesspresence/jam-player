#!/usr/bin/env python3
"""
JAM Player Setup Display

Displays the welcome/setup screen when the device is not yet provisioned.
Shows a QR code linking to the JAM Setup app for iOS/Android.

This screen is shown when:
- Device has completed first boot (credentials generated)
- Device is NOT yet registered with the JAM backend
"""

import sys
import os
import argparse
import subprocess
import io

from PIL import Image, ImageDraw, ImageFont

# Try to import qrcode, fall back gracefully if not available
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False
    print("Warning: qrcode module not available, QR code will not be displayed", file=sys.stderr)

# Display configuration
BACKGROUND_COLOR = (20, 20, 30)  # Dark blue-grey
TEXT_COLOR = (255, 255, 255)
ACCENT_COLOR = (0, 180, 255)  # JAM blue
SECONDARY_COLOR = (180, 180, 180)

FONT_SIZE_TITLE = 72
FONT_SIZE_SUBTITLE = 36
FONT_SIZE_INSTRUCTIONS = 32
FONT_SIZE_URL = 28

# App store URLs - update these with actual URLs
IOS_APP_URL = "https://apps.apple.com/app/jam-setup/id123456789"
ANDROID_APP_URL = "https://play.google.com/store/apps/details?id=com.justamenu.setup"
UNIVERSAL_SETUP_URL = "https://setup.justamenu.com"  # Redirects to appropriate store


def get_fb_size():
    """Get framebuffer dimensions."""
    try:
        with open('/sys/class/graphics/fb0/virtual_size', 'r') as f:
            w, h = f.read().strip().split(',')
            return int(w), int(h)
    except:
        return 1920, 1080


def get_font(size):
    """Get a font, falling back to default if needed."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def generate_qr_code(url, size=300):
    """Generate a QR code image for the given URL."""
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


def create_setup_image(width, height, device_id=None):
    """Create the setup/welcome screen image."""
    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = get_font(FONT_SIZE_TITLE)
    subtitle_font = get_font(FONT_SIZE_SUBTITLE)
    instructions_font = get_font(FONT_SIZE_INSTRUCTIONS)
    url_font = get_font(FONT_SIZE_URL)

    # Layout calculations
    center_x = width // 2
    qr_size = min(400, height // 3)

    # Start Y position (top third)
    y = height // 8

    # Title: "Welcome to JAM Player"
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
    y += bbox[3] + 80

    # QR Code (centered)
    qr_img = generate_qr_code(UNIVERSAL_SETUP_URL, qr_size)
    qr_x = center_x - qr_size // 2
    qr_y = y
    img.paste(qr_img, (qr_x, qr_y))
    y += qr_size + 40

    # Instructions below QR code
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

    y += 30

    # URL as fallback
    url_text = f"Or visit: {UNIVERSAL_SETUP_URL}"
    bbox = draw.textbbox((0, 0), url_text, font=url_font)
    x = center_x - bbox[2] // 2
    draw.text((x, y), url_text, font=url_font, fill=SECONDARY_COLOR)
    y += bbox[3] + 40

    # Device ID at bottom (for support)
    if device_id:
        device_text = f"Device ID: {device_id}"
        bbox = draw.textbbox((0, 0), device_text, font=url_font)
        x = center_x - bbox[2] // 2
        y = height - bbox[3] - 40
        draw.text((x, y), device_text, font=url_font, fill=SECONDARY_COLOR)

    return img


def wait_for_display(timeout=60):
    """Wait for X display to be available."""
    import time
    for _ in range(timeout):
        result = subprocess.run(
            ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'xdpyinfo'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if result.returncode == 0:
            return True
        time.sleep(1)
    return False


def display_image(img):
    """Display image fullscreen using feh."""
    # Save image to temp file
    img_path = '/tmp/jam_setup.png'
    img.save(img_path, 'PNG')
    os.chmod(img_path, 0o644)

    # Wait for graphical session to be ready
    if not wait_for_display(timeout=60):
        print("Warning: Display not available after 60s", file=sys.stderr)
        return

    # Run feh as the session owner (comitup), not root
    subprocess.Popen(
        ['sudo', '-u', 'comitup', 'env', 'DISPLAY=:0', 'feh', '-F', '--hide-pointer', img_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )


def main():
    parser = argparse.ArgumentParser(description='Display JAM Player setup screen')
    parser.add_argument('--uuid', '-u', help='Device UUID to display')
    parser.add_argument('--check-provisioned', '-c', action='store_true',
                        help='Exit silently if device is already provisioned')

    args = parser.parse_args()

    # If checking provisioned status, import and check
    if args.check_provisioned:
        try:
            # Add the common module path
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from common.credentials import is_device_provisioned, get_device_uuid

            if is_device_provisioned():
                print("Device is already provisioned, not showing setup screen")
                sys.exit(0)

            # Get device UUID if not provided
            if not args.uuid:
                args.uuid = get_device_uuid()

        except ImportError as e:
            print(f"Warning: Could not import credentials module: {e}", file=sys.stderr)

    # Get framebuffer size and create image
    width, height = get_fb_size()
    img = create_setup_image(width, height, device_id=args.uuid)

    # Display with feh
    display_image(img)
    print(f"Setup screen displayed (device: {args.uuid or 'unknown'})")


if __name__ == '__main__':
    main()
