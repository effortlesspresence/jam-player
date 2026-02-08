#!/usr/bin/env python3
"""
JAM Player Error Display

Displays critical error messages directly to framebuffer.
Uses Pillow (already installed) and writes directly to /dev/fb0.
No additional dependencies required.
"""

import sys
import os
import argparse
import subprocess

from PIL import Image, ImageDraw, ImageFont

# Display configuration
BACKGROUND_COLOR = (0, 0, 0)
TEXT_COLOR = (255, 255, 255)
ERROR_COLOR = (255, 100, 100)
FONT_SIZE_TITLE = 60
FONT_SIZE_MESSAGE = 40
FONT_SIZE_CONTACT = 30

DEFAULT_CONTACT = "Contact JAM support: support@justamenu.com"


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
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap_text(text, font, max_width, draw):
    """Wrap text to fit within max_width."""
    words = text.split()
    lines = []
    current_line = []

    for word in words:
        test_line = ' '.join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
            current_line = [word]

    if current_line:
        lines.append(' '.join(current_line))

    return lines


def create_error_image(width, height, title, message, device_uuid=None, contact=DEFAULT_CONTACT):
    """Create an error image."""
    img = Image.new('RGB', (width, height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = get_font(FONT_SIZE_TITLE)
    message_font = get_font(FONT_SIZE_MESSAGE)
    contact_font = get_font(FONT_SIZE_CONTACT)

    max_width = width - 200
    y = height // 4

    # Draw title
    for line in wrap_text(title, title_font, max_width, draw):
        bbox = draw.textbbox((0, 0), line, font=title_font)
        x = (width - bbox[2]) // 2
        draw.text((x, y), line, font=title_font, fill=ERROR_COLOR)
        y += bbox[3] + 20

    y += 60

    # Draw message
    for line in wrap_text(message, message_font, max_width, draw):
        bbox = draw.textbbox((0, 0), line, font=message_font)
        x = (width - bbox[2]) // 2
        draw.text((x, y), line, font=message_font, fill=TEXT_COLOR)
        y += bbox[3] + 15

    y += 60

    # Draw device UUID
    if device_uuid:
        uuid_text = f"Device ID: {device_uuid}"
        bbox = draw.textbbox((0, 0), uuid_text, font=contact_font)
        x = (width - bbox[2]) // 2
        draw.text((x, y), uuid_text, font=contact_font, fill=TEXT_COLOR)
        y += bbox[3] + 30

    # Draw contact info
    bbox = draw.textbbox((0, 0), contact, font=contact_font)
    x = (width - bbox[2]) // 2
    draw.text((x, y), contact, font=contact_font, fill=TEXT_COLOR)

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
    img_path = '/tmp/jam_error.png'
    img.save(img_path, 'PNG')
    os.chmod(img_path, 0o644)  # Ensure readable by all

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
    parser = argparse.ArgumentParser(description='Display critical error message on screen')
    parser.add_argument('message', nargs='?', help='Error message to display')
    parser.add_argument('--file', '-f', help='Read error from file')
    parser.add_argument('--title', '-t', default='JAM PLAYER ERROR', help='Error title')
    parser.add_argument('--uuid', '-u', help='Device UUID to display')
    parser.add_argument('--contact', '-c', default=DEFAULT_CONTACT, help='Contact information')

    args = parser.parse_args()

    # Get message from file if specified
    if args.file and os.path.exists(args.file):
        with open(args.file) as f:
            for line in f:
                if line.startswith('Error:'):
                    args.message = line.replace('Error:', '').strip()
                if line.startswith('Device UUID:') and not args.uuid:
                    args.uuid = line.replace('Device UUID:', '').strip()

    # Fall back to default error file
    if not args.message and os.path.exists('/etc/jam/boot_error.txt'):
        with open('/etc/jam/boot_error.txt') as f:
            for line in f:
                if line.startswith('Error:'):
                    args.message = line.replace('Error:', '').strip()

    if not args.message:
        print("Error: No message provided", file=sys.stderr)
        sys.exit(1)

    # Get framebuffer size and create image
    width, height = get_fb_size()
    img = create_error_image(width, height, args.title, args.message, args.uuid, args.contact)

    # Display with feh
    display_image(img)
    print(f"Error displayed: {args.title} - {args.message}")


if __name__ == '__main__':
    main()
