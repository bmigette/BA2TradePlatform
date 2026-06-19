"""
Logging Configuration Module

Configures Python logging with separate log files for different levels:
- debug.log: DEBUG and higher (all messages)
- info.log: INFO and higher (normal operations)
- error.log: ERROR and higher (errors and critical issues)

All loggers use the Python logging module with:
- Timestamp, level, module, and message format
- Log rotation configured for production use
- Python warnings are captured and routed to logs
"""

import logging
import logging.handlers
import warnings
from pathlib import Path
from typing import Optional


def setup_logging(
    log_dir: str = "logs",
    debug_log: str = "debug.log",
    info_log: str = "info.log",
    error_log: str = "error.log",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
    console_level: int = logging.INFO
) -> logging.Logger:
    """
    Set up logging with separate files for debug, info, and error levels.

    Args:
        log_dir: Directory for log files
        debug_log: Filename for debug log (DEBUG and higher)
        info_log: Filename for info log (INFO and higher)
        error_log: Filename for error log (ERROR and higher)
        max_bytes: Maximum size per log file before rotation
        backup_count: Number of backup files to keep
        console_level: Logging level for console output

    Returns:
        Root logger instance
    """
    # Create logs directory if it doesn't exist
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Define log format with timestamp, level, module, and message
    log_format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # Capture all levels

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Debug file handler - captures DEBUG and higher (all messages)
    debug_handler = logging.handlers.RotatingFileHandler(
        log_path / debug_log,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter)
    root_logger.addHandler(debug_handler)

    # Info file handler - captures INFO and higher
    info_handler = logging.handlers.RotatingFileHandler(
        log_path / info_log,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    root_logger.addHandler(info_handler)

    # Error file handler - captures ERROR and higher
    error_handler = logging.handlers.RotatingFileHandler(
        log_path / error_log,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Silence verbose third-party DEBUG loggers (keep WARNING and above visible)
    verbose_loggers = [
        'yfinance',
        'trafilatura.core',
        'trafilatura.readability_lxml',
        'trafilatura.external',
        'trafilatura.htmlprocessing',
        'trafilatura.main_extractor',
        'charset_normalizer',
        'filelock',
    ]
    for logger_name in verbose_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Capture Python warnings and route them to the logging system
    # This ensures warnings from libraries (e.g., backtesting.py) appear in logs
    logging.captureWarnings(True)
    warnings_logger = logging.getLogger('py.warnings')
    warnings_logger.setLevel(logging.WARNING)

    return root_logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


# Initialize logging when module is imported
_logging_initialized = False


def init_logging():
    """Initialize logging if not already done."""
    global _logging_initialized
    if not _logging_initialized:
        setup_logging()
        _logging_initialized = True
