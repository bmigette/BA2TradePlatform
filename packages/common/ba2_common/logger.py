import logging
from logging.handlers import RotatingFileHandler
from ba2_common.config import STDOUT_LOGGING, FILE_LOGGING, HOME, HOME_PARENT, LOG_FOLDER
import os
import io
import sys
import threading
import time
from contextlib import contextmanager
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

# --- Thread-scoped file-logging suppression (backtests) --------------------------------------
# File logging is for LIVE only. A backtest emits very high-throughput per-bar/per-expert logs; in
# the long-lived serve process re-runs execute in WORKER THREADS that share the rotating file
# handlers (app.log + the shared all.debug/all.error attached to every expert logger). Python's
# Logger.callHandlers iterates a logger's handlers WITHOUT the module lock, so when one thread's
# RotatingFileHandler rollover (or reconfigure_file_logging) CLOSES a handler stream while another
# re-run thread is mid-emit, that thread writes to a closed stream -> "I/O operation on closed file"
# (the exact failure that killed the persisted top-N re-runs). Rather than make the handlers
# multiprocess/thread rollover-safe, we simply skip FILE emission for the duration of a backtest on
# the CURRENT thread (STDOUT is untouched — it's captured in the serve log). Spawned GA pool workers
# already disable file logging entirely via BA2_FILE_LOGGING=0; this covers the in-process path.
_file_suppress = threading.local()


class _SuppressFileDuringBacktest(logging.Filter):
    """Drops a record from a FILE handler while the current thread is inside file_logging_disabled()."""
    def filter(self, record: logging.LogRecord) -> bool:  # True => keep, False => drop
        return not getattr(_file_suppress, "on", False)


# One shared filter instance attached to every rotating FILE handler we create (below).
_FILE_SUPPRESS_FILTER = _SuppressFileDuringBacktest()


@contextmanager
def file_logging_disabled():
    """Thread-scoped: suppress emission to the rotating FILE handlers for the duration (STDOUT keeps
    logging). Wrap backtest execution with this so in-process worker threads never race on
    RotatingFileHandler rollover/close. Nests safely; no-op cost when FILE_LOGGING is already off."""
    prev = getattr(_file_suppress, "on", False)
    _file_suppress.on = True
    try:
        yield
    finally:
        _file_suppress.on = prev


def _attach_suppress(handler: RotatingFileHandler) -> RotatingFileHandler:
    """Attach the backtest file-suppression filter to a rotating file handler (idempotent)."""
    if _FILE_SUPPRESS_FILTER not in handler.filters:
        handler.addFilter(_FILE_SUPPRESS_FILTER)
    return handler


# --- Rotation that survives a lost race + one handler per file path -------------------------
# 2026-07-28: once app.log crossed 10MB, EVERY subsequent record printed
#   --- Logging error --- / PermissionError: [WinError 32] ... app.log -> app.log.1
# On Windows os.rename fails while ANY other handle on the file is open. Two causes:
#   1. this module and ``ba2_trade_platform.logger`` are near-duplicates and EACH built its
#      own handler for the same app.log / app.debug.log / all.debug.log / all.error.log once
#      startup pointed both at <db folder>/logs -> two handles per file in ONE process, so
#      rotation could NEVER succeed and the files grew past maxBytes without bound.
#      Fixed by handing out one shared instance per path (get_shared_file_handler).
#   2. other PROCESSES write the same tree (ad-hoc scripts run from the repo, backtest/grid
#      workers). That race is unavoidable, so rotation must degrade instead of raising.
_ROTATE_COOLDOWN_SECONDS = 60.0


class SharedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps logging when it loses the rotation race.

    The stock handler lets the PermissionError escape ``doRollover``, which logging reports
    as "--- Logging error ---" with a full stack for every record. Here a failed rotation
    just leaves the current file in place (temporarily over maxBytes) and is retried after
    ``_ROTATE_COOLDOWN_SECONDS`` — no records are lost and stderr stays clean. The cooldown
    matters: ``shouldRollover`` keeps returning True on an oversized file, so without it
    every single record would retry the rename and the stream close/reopen.
    """

    def doRollover(self) -> None:
        now = time.time()
        if now < getattr(self, "_rotate_blocked_until", 0.0):
            return  # recent attempt lost the race; keep appending and retry later
        try:
            super().doRollover()
        except OSError as exc:
            # PermissionError/WinError 32 is the expected case; any OSError is treated the
            # same rather than letting a disk/FS problem take this process's logging down.
            self._rotate_blocked_until = now + _ROTATE_COOLDOWN_SECONDS
            self._rotate_last_error = exc
            if self.stream is None:
                # super() closes the stream BEFORE renaming, so reopen it (mode 'a') or the
                # next emit writes to None.
                self.stream = self._open()


_shared_file_handlers: dict = {}
_shared_handlers_lock = threading.Lock()


def get_shared_file_handler(path: str, level: int,
                            formatter: Optional[logging.Formatter] = None
                            ) -> Optional[RotatingFileHandler]:
    """The ONE handler for ``path`` in this process (created on first request).

    Both logger trees must attach the SAME object for a shared file, otherwise the file is
    held twice and rotation is permanently wedged (see the note above). ``level`` and
    ``formatter`` apply to the first caller; every file here has a fixed level
    (app.log INFO, app.debug.log DEBUG, all.error.log ERROR), so later callers for the same
    path want the same settings. Returns None when FILE_LOGGING is off.
    """
    if not FILE_LOGGING:
        return None
    abs_path = os.path.abspath(path)
    key = os.path.normcase(abs_path)
    with _shared_handlers_lock:
        handler = _shared_file_handlers.get(key)
        if handler is None:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            handler = SharedRotatingFileHandler(
                abs_path, maxBytes=(1024 * 1024 * 10), backupCount=7, encoding='utf-8')
            handler.setFormatter(formatter or logging.Formatter(LOG_FORMAT))
            handler.setLevel(level)
            _attach_suppress(handler)
            _shared_file_handlers[key] = handler
        return handler


def evict_shared_handlers_outside(log_dir: str) -> None:
    """Close + forget shared handlers that do not live under ``log_dir``.

    Used by ``reconfigure_file_logging`` when LOGS_DIR moves. Only handlers OUTSIDE the new
    directory are closed: both logger modules call reconfigure with the same target, and
    closing the freshly-built handlers on the second call would leave the first module's
    logger holding a closed stream ("I/O operation on closed file").
    """
    keep = os.path.normcase(os.path.abspath(log_dir))
    with _shared_handlers_lock:
        for key in [k for k in _shared_file_handlers
                    if os.path.normcase(os.path.dirname(k)) != keep]:
            try:
                _shared_file_handlers[key].close()
            except Exception:  # noqa: BLE001 — teardown must not raise
                pass
            del _shared_file_handlers[key]

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
    
    # One handler per path for the whole process (see get_shared_file_handler).
    _all_debug_handler = get_shared_file_handler(
        os.path.join(LOGS_DIR, "all.debug.log"), logging.DEBUG)

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
    
    # One handler per path for the whole process (see get_shared_file_handler).
    _all_error_handler = get_shared_file_handler(
        os.path.join(LOGS_DIR, "all.error.log"), logging.ERROR)

    return _all_error_handler

def _install_app_file_handlers() -> None:
    """Attach the rotating FILE handlers to the app logger under the CURRENT ``LOGS_DIR``:
    app.debug.log @ DEBUG, app.log @ INFO, plus the shared all.debug.log / all.error.log.
    No-op when FILE_LOGGING is off. Caller is responsible for removing any stale file handlers
    first (see ``reconfigure_file_logging``)."""
    if not FILE_LOGGING:
        return
    os.makedirs(LOGS_DIR, exist_ok=True)
    debug_fh = get_shared_file_handler(os.path.join(LOGS_DIR, "app.debug.log"), logging.DEBUG)
    if debug_fh:
        logger.addHandler(debug_fh)
    info_fh = get_shared_file_handler(os.path.join(LOGS_DIR, "app.log"), logging.INFO)
    if info_fh:
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
        
        file_handler = get_shared_file_handler(
            log_filepath, logging.DEBUG, formatter=file_formatter)
        if file_handler:
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

    # DETACH only — do NOT close here. These handlers are now shared per path across both
    # logger trees (see get_shared_file_handler), so closing one from this module would leave
    # ba2_trade_platform's logger holding a closed stream ("I/O operation on closed file").
    # Closing is the registry's job, and only for handlers outside the new directory.
    for lg in [logger, *_expert_loggers.values()]:
        for h in [h for h in lg.handlers if isinstance(h, RotatingFileHandler)]:
            lg.removeHandler(h)
    evict_shared_handlers_outside(log_dir)
    # Drop the shared singletons so they are re-resolved under the new dir.
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
        efh = get_shared_file_handler(
            os.path.join(LOGS_DIR, f"{expert_class}-exp{expert_id}.log"),
            logging.DEBUG, formatter=efmt)
        if efh:
            elog.addHandler(efh)
        if adh:
            elog.addHandler(adh)
        if aeh:
            elog.addHandler(aeh)


