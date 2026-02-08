import logging
from logging.handlers import WatchedFileHandler
import os.path


def get_logger(logger_name) -> logging.Logger:
    LOG_FILE = f"{logger_name}.log"
    ERR_FILE = f"{logger_name}.err"

    # Create a logger
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    # Create a file handler for writing regular logs
    log_file_handler = logging.FileHandler(
        os.path.join(os.path.expanduser("~"), LOG_FILE)
    )
    log_file_handler.setLevel(logging.INFO)

    # Create a file handler for writing error logs
    err_file_handler = WatchedFileHandler(
        os.path.join(os.path.expanduser("~"), ERR_FILE)
    )
    err_file_handler.setLevel(logging.ERROR)

    # Create a formatter and set it for both handlers
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%m-%d-%Y %H:%M:%S',
    )
    log_file_handler.setFormatter(formatter)
    err_file_handler.setFormatter(formatter)

    # Add the handlers to the logger
    logger.addHandler(log_file_handler)
    logger.addHandler(err_file_handler)

    return logger