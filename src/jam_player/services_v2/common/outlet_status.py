"""
Outlet (Location) operational status cache for JAM Player 2.0.

The backend computes a Location's operational status dynamically and
pushes the result to each registered JAM Player via:

  1. WebSocket SET_OUTLET_OPERATIONAL_STATUS commands (fast path, fires
     when an admin changes the location's activation/deactivation/
     scheduling state on the web app).

  2. The jam-outlet-status-poller.service (slow path, polls
     GET /jam-players/outlet-status every 6 minutes as a backstop for
     missed WS messages).

  3. The jam-heartbeat.service response (also picks up the current
     status as a side effect of its normal cadence).

All three writers funnel through this module. The status is stored as
JSON in EnumWithLabel shape to mirror the backend's wire format:

    {"value": "OFF_SEASON", "label": "Seasonal (Closed)"}

The "value" drives behavior (display mode selection); the "label" is
shown verbatim in the inactive-screen copy so we don't have to keep a
device-side enum-to-label mapping in sync with the backend's.

Enum values mirror LocationOperationalStatus on the backend:
  - OPERATIONAL    Outlet is active and within its operational window.
  - DEACTIVATED    Outlet has been deactivated by the customer.
  - OFF_SEASON     Outlet is seasonal and the current date is outside.
  - SCHEDULED      Outlet is temporary and the current date is before.
  - PERIOD_ENDED   Outlet is temporary and the current date is after.

This module never raises on missing or unreadable files: callers get
None instead, which they should interpret as "we don't know yet --
behave as if OPERATIONAL." That fail-open policy is intentional: a
read error on the cache file should not blank out a customer's
display.
"""

import json
import logging
from typing import Optional, Tuple

from .paths import (
    OUTLET_NAME_FILE,
    OUTLET_OPERATIONAL_STATUS_FILE,
    safe_write_text,
)

logger = logging.getLogger(__name__)

VALID_STATUSES = frozenset({
    "OPERATIONAL",
    "DEACTIVATED",
    "OFF_SEASON",
    "SCHEDULED",
    "PERIOD_ENDED",
})


def read_outlet_status() -> Optional[Tuple[str, str]]:
    """
    Return the cached (value, label) tuple, or None if no status has
    been written yet, the file is unreadable, the JSON is malformed,
    or the value is not in the recognized set. Unknown values are
    treated as None (fail-open) so a future enum addition on the
    backend doesn't crash old devices.
    """
    try:
        if not OUTLET_OPERATIONAL_STATUS_FILE.exists():
            return None
        raw = OUTLET_OPERATIONAL_STATUS_FILE.read_text().strip()
        if not raw:
            return None
        parsed = json.loads(raw)
        value = parsed.get("value")
        label = parsed.get("label")
        if not isinstance(value, str) or not isinstance(label, str):
            logger.warning(
                f"Malformed outlet status JSON in "
                f"{OUTLET_OPERATIONAL_STATUS_FILE}: {parsed!r}"
            )
            return None
        if value not in VALID_STATUSES:
            logger.warning(
                f"Unknown outlet status value {value!r} in "
                f"{OUTLET_OPERATIONAL_STATUS_FILE}, treating as None"
            )
            return None
        return (value, label)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read outlet status file: {e}")
        return None


def read_outlet_status_value() -> Optional[str]:
    """Convenience accessor returning just the raw enum value."""
    parsed = read_outlet_status()
    return parsed[0] if parsed else None


def read_outlet_status_label() -> Optional[str]:
    """Convenience accessor returning just the display label."""
    parsed = read_outlet_status()
    return parsed[1] if parsed else None


def write_outlet_status_if_changed(value: str, label: str) -> bool:
    """
    Write the new (value, label) to the cache file if it differs from
    the current cached value. Returns True if the file was actually
    written (status or label changed), False if it was a no-op.

    Callers should use the boolean return to decide whether to signal
    jam-player-display to redraw -- no point in waking the display
    service for a write that didn't change anything.
    """
    if value not in VALID_STATUSES:
        logger.warning(
            f"Refusing to write unknown outlet status value: {value!r}"
        )
        return False
    if not isinstance(label, str) or not label.strip():
        logger.warning(
            f"Refusing to write outlet status with empty label "
            f"(value={value!r})"
        )
        return False

    label = label.strip()
    current = read_outlet_status()
    if current == (value, label):
        return False

    try:
        payload = json.dumps({"value": value, "label": label})
        safe_write_text(OUTLET_OPERATIONAL_STATUS_FILE, payload)
        logger.info(
            f"Outlet status changed: {current} -> ({value!r}, {label!r})"
        )
        return True
    except OSError as e:
        logger.error(f"Could not write outlet status: {e}")
        return False


def read_outlet_name() -> Optional[str]:
    """Return the cached outlet name, or None if not yet written."""
    try:
        if not OUTLET_NAME_FILE.exists():
            return None
        value = OUTLET_NAME_FILE.read_text().strip()
        return value if value else None
    except OSError as e:
        logger.warning(f"Could not read outlet name file: {e}")
        return None


def write_outlet_name_if_changed(new_name: str) -> bool:
    """
    Write the new outlet name if changed. Returns True if written.
    Empty / whitespace-only names are ignored (we'd rather keep the
    old name than blank it out on a malformed payload).
    """
    if not new_name or not new_name.strip():
        return False
    new_name = new_name.strip()

    current = read_outlet_name()
    if current == new_name:
        return False

    try:
        safe_write_text(OUTLET_NAME_FILE, new_name)
        logger.info(f"Outlet name changed: {current!r} -> {new_name!r}")
        return True
    except OSError as e:
        logger.error(f"Could not write outlet name: {e}")
        return False


def is_outlet_operational() -> bool:
    """
    Return True if the cached status is OPERATIONAL *or* if no status
    has been cached yet (fail-open). This is what the display service
    should use to gate "show outlet inactive screen" decisions: it
    only suppresses content when we have a *definite* non-OPERATIONAL
    signal from the backend.
    """
    parsed = read_outlet_status()
    if parsed is None:
        return True
    return parsed[0] == "OPERATIONAL"
