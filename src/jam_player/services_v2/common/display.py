"""
JAM Player 2.0 - Display Utilities

Functions for displaying messages on the connected screen.
"""

import subprocess
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path to the error display script
DISPLAY_ERROR_SCRIPT = Path('/opt/jam/services/display_error.py')
PYTHON_EXECUTABLE = Path('/opt/jam/venv/bin/python')


def show_error_screen(
    message: str,
    title: str = "JAM PLAYER ERROR",
    device_uuid: Optional[str] = None,
    contact: str = "Contact JAM support: support@justamenu.com",
    block: bool = False
) -> Optional[subprocess.Popen]:
    """
    Display a critical error message on the connected screen.

    Launches display_error.py as a subprocess to render the error.
    By default, runs in background so the calling service can continue.

    Args:
        message: Error message to display
        title: Error title (displayed in red)
        device_uuid: Device UUID to display (helps with support)
        contact: Contact information
        block: If True, wait for the display process to exit

    Returns:
        Popen object if not blocking, None if blocking or on error
    """
    if not DISPLAY_ERROR_SCRIPT.exists():
        logger.error(f"Error display script not found: {DISPLAY_ERROR_SCRIPT}")
        return None

    if not PYTHON_EXECUTABLE.exists():
        logger.error(f"Python executable not found: {PYTHON_EXECUTABLE}")
        return None

    cmd = [
        str(PYTHON_EXECUTABLE),
        str(DISPLAY_ERROR_SCRIPT),
        '--title', title,
        '--contact', contact,
    ]

    if device_uuid:
        cmd.extend(['--uuid', device_uuid])

    # message is positional, must come last
    cmd.append(message)

    try:
        logger.info(f"Launching error display: {title}")

        if block:
            # Wait for the display to exit (probably never in normal use)
            subprocess.run(cmd, check=True)
            return None
        else:
            # Run in background
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True  # Detach from parent
            )
            logger.info(f"Error display started with PID {process.pid}")
            return process

    except Exception as e:
        logger.error(f"Failed to launch error display: {e}")
        return None


def show_error_from_file(
    error_file: Path = Path('/etc/jam/boot_error.txt'),
    block: bool = False
) -> Optional[subprocess.Popen]:
    """
    Display error from a file on the connected screen.

    Args:
        error_file: Path to error file
        block: If True, wait for the display process to exit

    Returns:
        Popen object if not blocking, None if blocking or on error
    """
    if not DISPLAY_ERROR_SCRIPT.exists():
        logger.error(f"Error display script not found: {DISPLAY_ERROR_SCRIPT}")
        return None

    if not PYTHON_EXECUTABLE.exists():
        logger.error(f"Python executable not found: {PYTHON_EXECUTABLE}")
        return None

    if not error_file.exists():
        logger.error(f"Error file not found: {error_file}")
        return None

    cmd = [
        str(PYTHON_EXECUTABLE),
        str(DISPLAY_ERROR_SCRIPT),
        '--file', str(error_file),
    ]

    try:
        logger.info(f"Launching error display from file: {error_file}")

        if block:
            subprocess.run(cmd, check=True)
            return None
        else:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            logger.info(f"Error display started with PID {process.pid}")
            return process

    except Exception as e:
        logger.error(f"Failed to launch error display: {e}")
        return None
