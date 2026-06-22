import logging
from logging.handlers import RotatingFileHandler
from ba2_common.config import STDOUT_LOGGING, FILE_LOGGING, HOME, HOME_PARENT, LOG_FOLDER
import os
import io
import sys
from typing import Optional

logger = logging.getLogger("ba2_common")
logger.setLevel(logging.DEBUG)

# Clear any existing handlers to prevent duplicates
logger.handlers.clear()

# Prevent propagation to root logger to avoid duplicate logs
logger.propagate = False

# Shared constants
LOG_FORMAT = '%(asctime)s - %(name)s - %(module)s - %(levelname)s - %(message)s'
# Import-time default lives under the BA2 data root (LOG_FOLDER = <BA2_HOME>/test/logs), NEVER in
# the code repo. Per-platform startup then relocates to <db folder>/logs via
# reconfigure_file_logging() (serve/live). The old `HOME_PARENT/logs` default wrote into
# packages/common/logs/ in the repo, which also signalled a process that hadn't resolved BA2_HOME.
LOGS_DIR = LOG_FOLDER

# Configure our handlers
formatter = logging.Formatter(LOG_FORMAT)

if STDOUT_LOGGING:
    # Create a safe StreamHandler that handles Unicode characters
    handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace'))
    handler.setLevel(logging.DEBUG) 
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Create shared handlers for all loggers (app + expert specific)
# These handlers will be added to both the app logger and all expert loggers
_all_debug_handler: Optional[RotatingFileHandler] = None
_all_error_handler: Optional[RotatingFileHandler] = None

def _get_all_debug_handler() -> Optional[RotatingFileHandler]:
    """
    Get or create the shared all.debug.log handler.
    This handler is used by all loggers (app logger and expert loggers).
    Captures all log levels (DEBUG and above).
    
    Returns:
        RotatingFileHandler: The shared handler, or None if FILE_LOGGING is disabled
    """
    global _all_debug_handler
    
    if _all_debug_handler is not None:
        return _all_debug_handler
    
    if not FILE_LOGGING:
        return None
    
    # Create the shared handler
    os.makedirs(LOGS_DIR, exist_ok=True)

    _all_debug_handler = RotatingFileHandler(
        os.path.join(LOGS_DIR, "all.debug.log"),
        maxBytes=(1024*1024*10),  # 10MB
        backupCount=7,
        encoding='utf-8'
    )
    
    _all_debug_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    _all_debug_handler.setLevel(logging.DEBUG)
    
    return _all_debug_handler

def _get_all_error_handler() -> Optional[RotatingFileHandler]:
    """
    Get or create the shared all.error.log handler.
    This handler is used by all loggers (app logger and expert loggers).
    Captures only ERROR level logs and above (ERROR, CRITICAL).
    
    Returns:
        RotatingFileHandler: The shared handler, or None if FILE_LOGGING is disabled
    """
    global _all_error_handler
    
    if _all_error_handler is not None:
        return _all_error_handler
    
    if not FILE_LOGGING:
        return None
    
    # Create the shared handler
    os.makedirs(LOGS_DIR, exist_ok=True)

    _all_error_handler = RotatingFileHandler(
        os.path.join(LOGS_DIR, "all.error.log"),
        maxBytes=(1024*1024*10),  # 10MB
        backupCount=7,
        encoding='utf-8'
    )
    
    _all_error_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    _all_error_handler.setLevel(logging.ERROR)  # Only ERROR and above
    
    return _all_error_handler

def _install_app_file_handlers() -> None:
    """Attach the rotating FILE handlers to the app logger under the CURRENT ``LOGS_DIR``:
    app.debug.log @ DEBUG, app.log @ INFO, plus the shared all.debug.log / all.error.log.
    No-op when FILE_LOGGING is off. Caller is responsible for removing any stale file handlers
    first (see ``reconfigure_file_logging``)."""
    if not FILE_LOGGING:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    fmt = logging.Formatter(LOG_FORMAT)
    debug_fh = RotatingFileHandler(
        os.path.join(LOGS_DIR, "app.debug.log"), maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
    )
    debug_fh.setFormatter(fmt)
    debug_fh.setLevel(logging.DEBUG)
    logger.addHandler(debug_fh)
    info_fh = RotatingFileHandler(
        os.path.join(LOGS_DIR, "app.log"), maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
    )
    info_fh.setFormatter(fmt)
    info_fh.setLevel(logging.INFO)
    logger.addHandler(info_fh)
    adh = _get_all_debug_handler()
    if adh:
        logger.addHandler(adh)
    aeh = _get_all_error_handler()
    if aeh:
        logger.addHandler(aeh)


# Add the file handlers to the main app logger at import (default LOGS_DIR).
_install_app_file_handlers()


# Cache for expert loggers to avoid recreation
_expert_loggers = {}


class ExpertFormatter(logging.Formatter):
    """Custom formatter that replaces logger name with expert class and instance ID."""
    
    def __init__(self, expert_class: str, expert_id: int, fmt_string: str):
        super().__init__(fmt_string)
        self.expert_prefix = f"[{expert_class}-{expert_id}]"
    
    def format(self, record):
        # Replace the logger name with expert prefix in the formatted output
        record.name = self.expert_prefix
        return super().format(record)


def get_expert_logger(expert_class: str, expert_id: int) -> logging.Logger:
    """
    Get or create a logger for a specific expert instance.
    
    Creates a logger that:
    - Logs to a file: logs/expert_class-expXX.log (e.g., TradingAgents-exp1.log)
    - Logs to STDOUT with prefix [EXPERTCLASS-ID] (e.g., [TradingAgents-1])
    - Uses the same formatter as the main app logger
    
    Args:
        expert_class: Expert class name (e.g., "TradingAgents", "FMPRating")
        expert_id: Expert instance ID
        
    Returns:
        logging.Logger: Configured logger for this expert instance
        
    Example:
        >>> expert_logger = get_expert_logger("TradingAgents", 5)
        >>> expert_logger.info("Analysis started")
        # Console: 2025-10-20 14:00:00 - [TradingAgents-5] Analysis started
        # File logs/TradingAgents-exp5.log: 2025-10-20 14:00:00 - tradingagents_exp5 - module - INFO - [TradingAgents-5] Analysis started
    """
    cache_key = f"{expert_class}-{expert_id}"
    
    # Return cached logger if it exists
    if cache_key in _expert_loggers:
        return _expert_loggers[cache_key]
    
    # Create new logger
    logger_name = f"{expert_class.lower()}_exp{expert_id}"
    expert_logger = logging.getLogger(logger_name)
    expert_logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers
    expert_logger.handlers.clear()
    
    # Prevent propagation to avoid duplicate logs
    expert_logger.propagate = False
    
    # Create custom formatter with expert prefix
    console_formatter = ExpertFormatter(expert_class, expert_id, LOG_FORMAT)
    file_formatter = ExpertFormatter(expert_class, expert_id, LOG_FORMAT)
    
    # Add console handler if STDOUT logging is enabled
    if STDOUT_LOGGING:
        console_handler = logging.StreamHandler(
            io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        )
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(console_formatter)
        expert_logger.addHandler(console_handler)
    
    # Add file handler if FILE logging is enabled
    if FILE_LOGGING:
        os.makedirs(LOGS_DIR, exist_ok=True)

        # Expert-specific log file: expert_class-expXX.log
        log_filename = f"{expert_class}-exp{expert_id}.log"
        log_filepath = os.path.join(LOGS_DIR, log_filename)
        
        file_handler = RotatingFileHandler(
            log_filepath,
            maxBytes=(1024*1024*10),  # 10MB
            backupCount=7,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        expert_logger.addHandler(file_handler)
        
        # Add the shared all.debug.log handler to this expert logger
        all_debug_handler = _get_all_debug_handler()
        if all_debug_handler:
            expert_logger.addHandler(all_debug_handler)
        
        # Add the shared all.error.log handler to this expert logger
        all_error_handler = _get_all_error_handler()
        if all_error_handler:
            expert_logger.addHandler(all_error_handler)
    
    # Cache the logger
    _expert_loggers[cache_key] = expert_logger

    return expert_logger


def reconfigure_file_logging(log_dir: str) -> None:
    """Repoint the rotating FILE handlers at ``log_dir`` (created if missing).

    Call ONCE per process after the instance's DB path is known (wired into
    ``ba2_common.core.db.configure_db``) so each instance writes to its OWN log folder —
    typically ``<db folder>/logs`` — instead of every instance sharing
    ``BA2TradeCommon/logs`` and racing on RotatingFileHandler rollover (Windows WinError 32
    renaming ``all.debug.log`` -> ``all.debug.log.1`` while another process holds it open).

    No-op when FILE_LOGGING is off (e.g. spawned optimizer workers, which set
    BA2_FILE_LOGGING=0) or when already pointed at ``log_dir``. STDOUT handlers are untouched.
    """
    global LOGS_DIR, _all_debug_handler, _all_error_handler
    if not FILE_LOGGING:
        return
    log_dir = os.path.abspath(log_dir)
    if log_dir == os.path.abspath(LOGS_DIR):
        return

    # Detach + close every rotating file handler currently attached to the app logger and any
    # already-created expert loggers (the shared all.* singletons are among them).
    for lg in [logger, *_expert_loggers.values()]:
        for h in [h for h in lg.handlers if isinstance(h, RotatingFileHandler)]:
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    # Drop the shared singletons so they are rebuilt under the new dir.
    _all_debug_handler = None
    _all_error_handler = None

    LOGS_DIR = log_dir
    _install_app_file_handlers()

    # Rebuild each already-created expert logger's own file handler + reattach the shared ones.
    adh = _get_all_debug_handler()
    aeh = _get_all_error_handler()
    for cache_key, elog in list(_expert_loggers.items()):
        expert_class, _, expert_id = cache_key.rpartition("-")
        if not expert_class:
            continue
        efmt = ExpertFormatter(expert_class, expert_id, LOG_FORMAT)
        efh = RotatingFileHandler(
            os.path.join(LOGS_DIR, f"{expert_class}-exp{expert_id}.log"),
            maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
        )
        efh.setLevel(logging.DEBUG)
        efh.setFormatter(efmt)
        elog.addHandler(efh)
        if adh:
            elog.addHandler(adh)
        if aeh:
            elog.addHandler(aeh)


