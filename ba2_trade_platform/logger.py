import logging
from logging.handlers import RotatingFileHandler
from .config import STDOUT_LOGGING, FILE_LOGGING, HOME, HOME_PARENT
import os
import io
import sys
from typing import Optional

logger = logging.getLogger("ba2_trade_platform")
logger.setLevel(logging.DEBUG)

# Clear any existing handlers to prevent duplicates
logger.handlers.clear()

# Prevent propagation to root logger to avoid duplicate logs
logger.propagate = False

# Configure our handlers
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(module)s - %(levelname)s - %(message)s')

if STDOUT_LOGGING:
    # Create a safe StreamHandler that handles Unicode characters
    handler = logging.StreamHandler(io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace'))
    handler.setLevel(logging.DEBUG) 
    handler.setFormatter(formatter)
    logger.addHandler(handler)

if FILE_LOGGING:
    # Ensure logs directory exists
    logs_dir = os.path.join(HOME_PARENT, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    handlerfile = RotatingFileHandler(
        os.path.join(logs_dir, "app.debug.log"), maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
    )
    handlerfile.setFormatter(formatter)
    handlerfile.setLevel(logging.DEBUG)
    logger.addHandler(handlerfile)
    handlerfile2 = RotatingFileHandler(
        os.path.join(logs_dir, "app.log"), maxBytes=(1024*1024*10), backupCount=7, encoding='utf-8'
    )
    handlerfile2.setFormatter(formatter)
    handlerfile2.setLevel(logging.INFO)
    logger.addHandler(handlerfile2)


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
    
    # Base format string
    fmt_string = '%(asctime)s - %(name)s - %(module)s - %(levelname)s - %(message)s'
    
    # Create custom formatter with expert prefix for console
    console_formatter = ExpertFormatter(expert_class, expert_id, fmt_string)
    
    # File formatter (same as console)
    file_formatter = ExpertFormatter(expert_class, expert_id, fmt_string)
    
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
        logs_dir = os.path.join(HOME_PARENT, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        
        # Expert-specific log file: expert_class-expXX.log
        log_filename = f"{expert_class}-exp{expert_id}.log"
        log_filepath = os.path.join(logs_dir, log_filename)
        
        file_handler = RotatingFileHandler(
            log_filepath,
            maxBytes=(1024*1024*10),  # 10MB
            backupCount=7,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        expert_logger.addHandler(file_handler)
    
    # Cache the logger
    _expert_loggers[cache_key] = expert_logger
    
    return expert_logger


