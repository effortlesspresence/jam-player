"""
JAM Player 2.0 - Centralized Logging Configuration

Provides consistent logging setup across all JP 2.0 services.
"""

import logging
from typing import Optional

DEFAULT_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
DEFAULT_LOG_LEVEL = logging.INFO


def setup_service_logging(
    service_name: str,
    level: int = DEFAULT_LOG_LEVEL,
    log_format: str = DEFAULT_LOG_FORMAT
) -> logging.Logger:
    """
    Setup logging for a JAM Player service.

    This provides a consistent logging configuration across all services,
    avoiding duplication of basicConfig and getLogger calls.

    Args:
        service_name: Name of the service (used as logger name, e.g., 'jam-announce')
        level: Logging level (default INFO)
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
    logging.basicConfig(level=level, format=log_format)
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
