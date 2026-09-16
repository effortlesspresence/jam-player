"""
JAM Player 2.0 - Centralized Logging Configuration

Every jam-* service calls setup_service_logging('<service-name>') once at
import. It wires the root logger for the split in docs/LOGGING.md:

  - Local (stderr -> journald -> the SD card): one StreamHandler pinned at
    WARNING. INFO and DEBUG are never handed to journald, so they never
    touch the card. Lines carry systemd's "<N>" priority prefix
    (common/journal.py) so journald stores them at the right priority;
    without it every line counts as "info" and the drop-in's
    MaxLevelStore=warning would discard the WARNINGs too.
  - Backend (POST /jam-players/logs): one BackendLogHandler
    (common/log_shipper.py) at the configured level, tagged with the
    service's jam_player_system_service enum value. In-memory batches,
    dropped when the player is offline; nothing is queued on disk.

The root logger's level -- what /etc/jam/config/log_level controls -- now
means "what is SHIPPED". DEBUG there sends DEBUG lines to the backend and
writes nothing extra to the card.

Calling setup_service_logging() more than once in a process is safe: the
handlers are found and re-levelled, not duplicated, and the first call's
service tag is kept (jam_display_cache_prewarm imports jam_player_display,
and jam_update imports it lazily; the process's own service must win).

=============================================================================
HOW TO ENABLE DEBUG LOGGING ON A LIVE DEVICE
=============================================================================

The file /etc/jam/config/log_level (if present) overrides the default INFO
level. Valid contents: DEBUG, INFO, WARNING, ERROR, CRITICAL. Missing,
empty, or unrecognized values fall back to INFO.

Changes only take effect when the affected service restarts. To flip every
JAM service on a device into DEBUG:

    echo DEBUG | sudo tee /etc/jam/config/log_level
    sudo systemctl restart 'jam-*.service'

To flip back:

    echo INFO | sudo tee /etc/jam/config/log_level
    sudo systemctl restart 'jam-*.service'

Or just delete the file and reboot:

    sudo rm /etc/jam/config/log_level
    sudo reboot

DEBUG costs nothing on the card. It costs backend volume (the server caps
a player at 600 lines per rolling minute and drops the rest), so flip back
when done. The lines show up in the player's Logs & Errors dashboard panel.
=============================================================================
"""

import logging
import sys
import threading
from typing import Dict, Optional, Type, TypeVar

from .paths import LOG_LEVEL_FILE

DEFAULT_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
DEFAULT_LOG_LEVEL = logging.INFO

# What the local stream handler (the card) accepts. Fixed by docs/LOGGING.md;
# not affected by /etc/jam/config/log_level.
LOCAL_STREAM_LEVEL = logging.WARNING

# Accepted (case-insensitive) values in /etc/jam/config/log_level.
_VALID_LEVEL_NAMES = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}

# Service logger name -> jam_player_system_service enum value shipped with
# every log line. The table in docs/LOGGING.md is the contract; anything
# not listed ships as OTHER.
SERVICE_TO_SYSTEM_SERVICE: Dict[str, str] = {
    'jam-announce': 'JAM_ANNOUNCE',
    'jam-ble-provisioning': 'JAM_BLE_PROVISIONING',
    'jam-ble-state-manager': 'JAM_BLE_STATE_MANAGER',
    'jam-boot-check': 'JAM_BOOT_CHECK',
    'jam-first-boot': 'JAM_FIRST_BOOT',
    'jam-health-monitor': 'JAM_HEALTH_MONITOR',
    'jam-heartbeat': 'JAM_HEARTBEAT',
    'jam-player-display': 'JAM_PLAYER_DISPLAY',
    'jam-registration-poller': 'JAM_REGISTRATION_POLLER',
    'jam-tailscale': 'JAM_TAILSCALE',
    'jam-update': 'JAM_UPDATE',
    'jam-ws-commands': 'JAM_WEBSOCKET_COMMANDS',
    'jam-chrony-peering': 'JAM_CHRONY_PEERING',
    'jam-display-cache-prewarm': 'JAM_DISPLAY_CACHE_PREWARM',
    'jam-display-hotplug-monitor': 'JAM_DISPLAY_HOTPLUG_MONITOR',
    'jam-display-wait-for-hdmi': 'JAM_DISPLAY_WAIT_FOR_HDMI',
    'jam-installed-version-reporter': 'JAM_INSTALLED_VERSION_REPORTER',
    'jam-outlet-status-poller': 'JAM_OUTLET_STATUS_POLLER',
    'jam-venv-repair': 'JAM_VENV_REPAIR',
    'jam-content-manager': 'JAM_CONTENT_MANAGER',
}

_HandlerT = TypeVar('_HandlerT', bound=logging.Handler)


def service_enum_for(service_name: str) -> str:
    """The jam_player_system_service value for a service logger name (OTHER if unmapped)."""
    return SERVICE_TO_SYSTEM_SERVICE.get(service_name, 'OTHER')


def _resolve_level() -> int:
    """
    Read /etc/jam/config/log_level and return the corresponding logging level.

    Falls back to DEFAULT_LOG_LEVEL (INFO) for any of:
      - file does not exist
      - file is empty or whitespace-only
      - file contains an unrecognized level name
      - file is unreadable for any reason

    We intentionally never raise from here -- a misconfigured log_level
    file should never prevent a service from starting.
    """
    try:
        if not LOG_LEVEL_FILE.exists():
            return DEFAULT_LOG_LEVEL
        raw = LOG_LEVEL_FILE.read_text().strip().upper()
    except Exception:
        return DEFAULT_LOG_LEVEL

    if not raw or raw not in _VALID_LEVEL_NAMES:
        return DEFAULT_LOG_LEVEL

    resolved = getattr(logging, raw, None)
    return resolved if isinstance(resolved, int) else DEFAULT_LOG_LEVEL


def _find_handler(logger: logging.Logger, handler_type: Type[_HandlerT]) -> Optional[_HandlerT]:
    for handler in logger.handlers:
        if isinstance(handler, handler_type):
            return handler
    return None


def _called_from_main_module() -> bool:
    """True when setup_service_logging() was called from the __main__ module."""
    try:
        return sys._getframe(2).f_globals.get('__name__') == '__main__'
    except Exception:
        return False


def setup_service_logging(
    service_name: str,
    level: Optional[int] = None,
    log_format: str = DEFAULT_LOG_FORMAT
) -> logging.Logger:
    """
    Setup logging for a JAM Player service.

    Installs (once per process) the two root handlers described in the
    module docstring and sets the root level. Safe to call repeatedly.

    Args:
        service_name: Name of the service (used as logger name, e.g., 'jam-announce',
            and mapped to the backend service enum via service_enum_for()).
        level: Logging level. If None (the default), reads
            /etc/jam/config/log_level, falling back to INFO. This is the
            level that is shipped to the backend; the card stays at WARNING.
        log_format: Log format string for the local (card) lines.

    Returns:
        Configured logger instance

    Example:
        from common.logging_config import setup_service_logging, log_service_start

        logger = setup_service_logging('jam-announce')

        def main():
            log_service_start(logger, 'JAM Announce Service')
            # ... service logic
    """
    effective_level = level if level is not None else _resolve_level()
    root = logging.getLogger()
    root.setLevel(effective_level)

    # BackendLogHandler subclasses logging.Handler directly, so the two
    # isinstance checks below never match the same object.
    # These pull in the signed-request stack (requests, nacl) transitively.
    # jam-display-wait-for-hdmi and jam-venv-repair must keep working on a
    # venv where those are half-written (the case venv-repair exists for),
    # so degrade to a plain stream handler instead of failing to start.
    try:
        from .journal import JournalPriorityFormatter, stderr_is_journal
        from .log_shipper import BackendLogHandler, ShipperInternalFilter
        shipping_available = True
    except Exception as import_error:  # pragma: no cover - venv damage
        JournalPriorityFormatter = None  # type: ignore[assignment]
        stderr_is_journal = lambda: False  # type: ignore[assignment]  # noqa: E731
        BackendLogHandler = None  # type: ignore[assignment]
        ShipperInternalFilter = None  # type: ignore[assignment]
        shipping_available = False
        sys.stderr.write(f"<4>[logging_config] backend log shipping unavailable: {import_error}\n")

    stream_handler = _find_handler(root, logging.StreamHandler)
    if stream_handler is None:
        stream_handler = logging.StreamHandler()  # stderr -> journald
        root.addHandler(stream_handler)
    # Always (re)apply the formatter and filter, also to a handler some import
    # installed first (e.g. a stray basicConfig): an unprefixed WARNING is
    # stored by journald as info and dropped, which would silently defeat the
    # whole on-card rule for the process.
    stream_handler.setFormatter(JournalPriorityFormatter(log_format) if shipping_available else logging.Formatter(log_format))
    if shipping_available and not any(isinstance(f, ShipperInternalFilter) for f in stream_handler.filters):
        stream_handler.addFilter(ShipperInternalFilter())
    # Under journald the local stream is the SD card: WARNING+ only. Run by
    # hand in a terminal (no journal stream) it is a person's screen, and the
    # effective level applies so on-site debugging still sees INFO/DEBUG.
    stream_handler.setLevel(LOCAL_STREAM_LEVEL if stderr_is_journal() else effective_level)

    backend_handler = _find_handler(root, BackendLogHandler) if shipping_available else None
    if backend_handler is None and shipping_available:
        backend_handler = BackendLogHandler(service=service_enum_for(service_name))
    elif backend_handler is not None and _called_from_main_module() and backend_handler.service != service_enum_for(service_name):
        # The PROCESS's own service must win, whatever ran first. Services
        # import each other for shared code (jam_display_cache_prewarm imports
        # jam_player_display, whose module-level setup_service_logging runs
        # before prewarm's own), so "first call wins" mis-attributed every
        # line of the importing process to the imported module's service.
        # Only the __main__ module re-tags; a module imported lazily later
        # (jam_update imports jam_player_display inside a function) cannot
        # steal the tag from the running service.
        backend_handler.service = service_enum_for(service_name)
    # Attach on EVERY path, not just the re-tag branch. From 2026-09-11 to
    # 2026-09-16 this line sat one indent deeper, inside the elif above, so a
    # freshly created handler (the normal first call in every service) was
    # configured, levelled and then never added to the root logger: not one
    # line reached POST /jam-players/logs from any player on that code.
    if backend_handler is not None and backend_handler not in root.handlers:
        root.addHandler(backend_handler)
    if backend_handler is not None:
        backend_handler.setLevel(effective_level)

    _install_uncaught_exception_hooks(service_name)
    return logging.getLogger(service_name)


def _install_uncaught_exception_hooks(service_name: str) -> None:
    """
    Route uncaught exceptions through logging.

    The interpreter's default traceback print goes to stderr UNPREFIXED, so
    journald tags it info and MaxLevelStore=warning drops it: a service
    crash-looping on an uncaught exception would leave no cause on the card
    and nothing in the backend. Through the logger it is a CRITICAL record:
    priority 2 on the card, shipped when online.
    """
    if getattr(sys, '_jam_excepthook_installed', False):
        return
    service_logger = logging.getLogger(service_name)

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        try:
            service_logger.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        except Exception:
            sys.__excepthook__(exc_type, exc, tb)

    def threading_excepthook(args):
        try:
            service_logger.critical(
                f"Uncaught exception in thread {getattr(args.thread, 'name', '?')}",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:
            pass

    sys.excepthook = excepthook
    threading.excepthook = threading_excepthook
    sys._jam_excepthook_installed = True


def log_service_start(logger: logging.Logger, service_name: str) -> None:
    """
    Log the standard service startup banner.

    Provides consistent startup logging across all services.

    Args:
        logger: Logger instance to use
        service_name: Human-readable service name for the banner
    """
    logger.info("=" * 60)
    logger.info(f"{service_name} Starting")
    logger.info("=" * 60)


def log_service_ready(logger: logging.Logger, service_name: str, status_msg: Optional[str] = None) -> None:
    """
    Log that a service is ready.

    Args:
        logger: Logger instance to use
        service_name: Human-readable service name
        status_msg: Optional additional status message
    """
    if status_msg:
        logger.info(f"{service_name} ready - {status_msg}")
    else:
        logger.info(f"{service_name} ready")
