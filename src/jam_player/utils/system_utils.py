import subprocess
import re
import os
from jam_player.utils import logging_utils as lu

logger = lu.get_logger("utils.system_utils")


class ScreenConfig:
    def __init__(self, width: int, height: int, orientation: str):
        self.base_width = width
        self.base_height = height
        self.orientation = orientation

        # Adjust dimensions based on orientation
        if orientation in ["PORTRAIT_BOTTOM_ON_LEFT", "PORTRAIT_BOTTOM_ON_RIGHT"]:
            self.width = min(width, height)
            self.height = max(width, height)
        else:  # LANDSCAPE
            self.width = max(width, height)
            self.height = min(width, height)

    def get_pygame_rotation(self) -> int:
        """Get rotation angle for pygame transforms"""
        rotations = {
            "LANDSCAPE": 0,
            "PORTRAIT_BOTTOM_ON_LEFT": 270,
            "PORTRAIT_BOTTOM_ON_RIGHT": 90
        }
        return rotations[self.orientation]

    def get_display_dimensions(self) -> tuple[int, int]:
        """Get dimensions for setting up the display"""
        if self.orientation in ["PORTRAIT_BOTTOM_ON_LEFT", "PORTRAIT_BOTTOM_ON_RIGHT"]:
            return (self.base_height, self.base_width)  # Swapped for portrait
        return (self.base_width, self.base_height)


def get_screen_config(orientation: str) -> ScreenConfig:
    """Get screen configuration including resolution and orientation."""
    try:
        output = subprocess.check_output(
            'DISPLAY=:0 xdpyinfo', shell=True, text=True, encoding='utf-8'
        )
        match = re.search(r'dimensions:\s+(\d+)x(\d+)', output)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            logger.info(f"Detected screen resolution: {width}x{height}")
        else:
            width, height = 3840, 2160
            logger.info("Could not detect screen resolution. Defaulting to 4K.")
    except Exception as e:
        width, height = 3840, 2160
        logger.exception("Error detecting screen resolution. Defaulting to 4K.", exc_info=True)

    return ScreenConfig(width, height, orientation)


def run_cmd(cmd) -> str:
    output = subprocess.check_output(
        cmd, shell=True, text=True, encoding='utf-8'
    )
    return output


def file_exists_or_prefix_exists(file_path):
    # Check if the exact file exists
    if os.path.exists(file_path):
        return file_path

    # Extract directory and file prefix
    directory, file_prefix = os.path.split(file_path)

    # Check if the directory exists
    if not os.path.isdir(directory):
        return False

    # Check for any file in the directory that starts with the file prefix
    for filename in os.listdir(directory):
        if filename.startswith(file_prefix):
            return os.path.join(directory, filename)

    return False