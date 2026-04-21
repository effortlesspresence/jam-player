"""
JAM Player 2.0 - Centralized Logging Configuration

Provides consistent logging setup across all JP 2.0 services.

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

Reminder: DEBUG output counts against the 1 GB journal cap. Don't leave a
device in DEBUG permanently -- flip back (or reboot) when done.
=============================================================================
"""

import logging
from typing import Optional

from .paths import LOG_LEVEL_FILE

DEFAULT_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
DEFAULT_LOG_LEVEL = logging.INFO

# Accepted (case-insensitive) values in /etc/jam/config/log_level.
_VALID_LEVEL_NAMES = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}


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


def setup_service_logging(
    service_name: str,
    level: Optional[int] = None,
    log_format: str = DEFAULT_LOG_FORMAT
) -> logging.Logger:
    """
    Setup logging for a JAM Player service.

    This provides a consistent logging configuration across all services,
    avoiding duplication of basicConfig and getLogger calls.

    Args:
        service_name: Name of the service (used as logger name, e.g., 'jam-announce')
        level: Logging level. If None (the default), reads
            /etc/jam/config/log_level, falling back to INFO.
        log_format: Log format string (uses default if not specified)

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
    logging.basicConfig(level=effective_level, format=log_format)

    # basicConfig is a no-op if another import already configured the root
    # logger. Explicitly setting the root level guarantees our level wins
    # regardless of import order.
    logging.getLogger().setLevel(effective_level)

    return logging.getLogger(service_name)


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
