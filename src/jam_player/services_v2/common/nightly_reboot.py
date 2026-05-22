"""
Per-device nightly reboot time picker.

JAM Players reboot themselves every night to apply updates and clear
accumulated state. Historically every device rebooted at exactly 03:00
local time, which meant the entire fleet hit the backend at the same
moment with a surge of heartbeat / installed-version / outlet-status
calls -- especially painful since most customers share a single
timezone.

This module picks a random reboot time per device within a fixed
window (01:45 -- 04:15 local), persists it to disk, and serves it back
to jam_update.py for cron-line generation. The pick is done ONCE per
device (first jam-update run after the new code is installed) and
stays stable forever after, so the reboot time on a given device is
predictable for support diagnostics.

To force re-randomization on a specific device, support can delete
/etc/jam/device_data/nightly_reboot_time.txt -- the next jam-update
run will pick a new time.
"""

import logging
import os
import random
import re
import subprocess
from pathlib import Path
from typing import Tuple

from .paths import NIGHTLY_REBOOT_TIME_FILE, safe_write_text

logger = logging.getLogger(__name__)


# Reboot-time window, in minutes-since-midnight.
#   01:45 = 105
#   04:15 = 255
# Inclusive on the start, EXCLUSIVE on the end, so 04:15 is never picked.
# 150-minute window with per-minute granularity = up to 150 distinct slots.
_WINDOW_START_MINUTE = 1 * 60 + 45  # 01:45
_WINDOW_END_MINUTE = 4 * 60 + 15    # 04:15 (exclusive)

# Pattern for a valid persisted time: HH:MM with two digits each.
# We only accept HH in [00, 23] and MM in [00, 59] when parsing.
_TIME_RE = re.compile(r'^([0-9]{2}):([0-9]{2})$')


def _format(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _parse(value: str) -> Tuple[int, int]:
    """
    Parse a "HH:MM" string. Raises ValueError on any malformation
    (wrong format, out-of-range hour/minute). Caller handles the
    fallback to re-pick.
    """
    match = _TIME_RE.match(value.strip())
    if not match:
        raise ValueError(f"Not in HH:MM format: {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Hour/minute out of range: {value!r}")
    return hour, minute


def _pick_random_time() -> Tuple[int, int]:
    """
    Pick a fresh random reboot time inside the window. Uses
    random.SystemRandom() (backed by os.urandom) for true entropy --
    we don't want all devices manufactured in the same batch to land
    on the same seed and pick the same time.
    """
    rng = random.SystemRandom()
    minute_of_day = rng.randrange(_WINDOW_START_MINUTE, _WINDOW_END_MINUTE)
    return divmod(minute_of_day, 60)  # (hour, minute)


def read_reboot_time() -> Tuple[int, int] | None:
    """
    Return the cached (hour, minute) tuple, or None if no value has
    been written yet OR the file is unreadable / malformed. None means
    the caller should pick a new time.
    """
    try:
        if not NIGHTLY_REBOOT_TIME_FILE.exists():
            return None
        return _parse(NIGHTLY_REBOOT_TIME_FILE.read_text())
    except (OSError, ValueError) as e:
        logger.warning(
            f"Could not read {NIGHTLY_REBOOT_TIME_FILE}: {e}. "
            f"Will re-pick on next call."
        )
        return None


def get_or_pick_reboot_time() -> Tuple[int, int]:
    """
    Return the cached reboot time. If none exists (first run after
    install, or support deleted the file), pick a new random one,
    persist it, and return that.

    Idempotent on the file: subsequent calls return the same value
    without re-picking. Devices keep their assigned slot for life
    unless support explicitly clears the cache.
    """
    cached = read_reboot_time()
    if cached is not None:
        return cached

    hour, minute = _pick_random_time()
    formatted = _format(hour, minute)
    try:
        # Make sure the device_data dir exists. jam-first-boot
        # normally creates it, but this module may run very early.
        NIGHTLY_REBOOT_TIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(NIGHTLY_REBOOT_TIME_FILE, formatted + "\n")
        logger.info(
            f"Picked nightly reboot time {formatted} (local) -- "
            f"persisted to {NIGHTLY_REBOOT_TIME_FILE}"
        )
    except OSError as e:
        # If we can't persist, fall through with the picked value
        # anyway -- the cron line will still be installed for THIS
        # update cycle. Next update will re-pick (since we couldn't
        # write the file), which is acceptable.
        logger.error(
            f"Could not persist reboot time to "
            f"{NIGHTLY_REBOOT_TIME_FILE}: {e}. Using picked value "
            f"{formatted} for this run only."
        )

    return hour, minute


def render_and_install_crontab(template_path: Path) -> bool:
    """
    Render the JAM crontab template with this device's reboot time and
    install it as root's crontab.

    Used by jam-first-boot (primary, runs once per device on the very
    first boot) and jam-update (re-installs when a real update is
    happening, so future template changes propagate).

    Steps:
      1. Read the template from `template_path`.
      2. Get/pick this device's nightly reboot time (HH:MM).
      3. Substitute {REBOOT_HOUR} / {REBOOT_MINUTE} placeholders.
      4. Refuse to install if any placeholder is still present (a
         reverted template would otherwise corrupt cron).
      5. Pipe the rendered text to `crontab -` as root.

    Returns True on success, False on any failure (template missing,
    crontab command failed, etc.). On failure to pick the reboot time
    via the persistence path, falls back to 03:00 so the device still
    has SOME nightly reboot scheduled rather than none at all.
    """
    if not template_path.exists():
        logger.warning(f"Crontab template not found: {template_path}")
        return False

    try:
        hour, minute = get_or_pick_reboot_time()
    except Exception as e:
        # Defensive fallback: a broken picker must not leave the device
        # without a nightly reboot. Log loudly and fall back to 03:00.
        logger.error(
            f"nightly_reboot picker failed ({e}); falling back to 03:00"
        )
        hour, minute = 3, 0

    try:
        template = template_path.read_text()
    except OSError as e:
        logger.warning(f"Failed to read crontab template: {e}")
        return False

    rendered = (
        template
        .replace('{REBOOT_HOUR}', str(hour))
        .replace('{REBOOT_MINUTE}', str(minute))
    )

    # Ensure the rendered crontab ends with a newline -- `crontab -`
    # rejects input that doesn't ("new crontab file is missing newline
    # before EOF, can't install"). Our template's last line is the
    # reboot entry and may not have a trailing newline in the source
    # file, so we normalize here defensively.
    if not rendered.endswith('\n'):
        rendered += '\n'

    # Sanity check: refuse to install a template that still has
    # unsubstituted placeholders. Catches a reverted template file --
    # a literal "{REBOOT_HOUR}" in the crontab would crash cron.
    if '{REBOOT_HOUR}' in rendered or '{REBOOT_MINUTE}' in rendered:
        logger.error(
            "Crontab template still contains unsubstituted "
            "{REBOOT_HOUR}/{REBOOT_MINUTE} placeholders -- refusing "
            "to install."
        )
        return False

    try:
        result = subprocess.run(
            ['crontab', '-'],
            input=rendered,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info(
                f"Installed crontab (nightly reboot at "
                f"{hour:02d}:{minute:02d} local)"
            )
            return True
        logger.warning(
            f"`crontab -` returned {result.returncode}: "
            f"{result.stderr.strip()}"
        )
        return False
    except Exception as e:
        logger.warning(f"Failed to install crontab: {e}")
        return False
