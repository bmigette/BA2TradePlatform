"""
TradingAgents Logger System
Provides expert-specific logging with both console and file output
"""
import logging
import os
import sys
from datetime import datetime
from typing import Optional
from logging.handlers import RotatingFileHandler

try:
    from ba2_trade_platform import config as ba2_config
except ImportError:
    # Fallback if BA2 config is not available
    class MockConfig:
        STDOUT_LOGGING = True
        FILE_LOGGING = True
    ba2_config = MockConfig()


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors and icons to log messages"""
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[92m',     # Green
        'WARNING': '\033[93m',  # Yellow
        'ERROR': '\033[91m',    # Red
        'CRITICAL': '\033[95m', # Magenta
        'RESET': '\033[0m'      # Reset
    }
    
    # Icons for different log levels (fallback to ASCII if Unicode not supported)
    ICONS = {
        'DEBUG': '[DEBUG]',
        'INFO': '[INFO] ',
        'WARNING': '[WARN] ',
        'ERROR': '[ERROR]',
        'CRITICAL': '[CRIT]'
    }
    
    # Try to use Unicode icons if console supports it
    try:
        # Test Unicode support
        '\U0001f50d'.encode(sys.stdout.encoding or 'utf-8')
        ICONS = {
            'DEBUG': '🔍',
            'INFO': 'ℹ️ ',
            'WARNING': '⚠️ ',
            'ERROR': '❌',
            'CRITICAL': '🚨'
        }
    except (UnicodeEncodeError, AttributeError):
        # Fallback to ASCII icons
        pass
    
    def format(self, record):
        # Add color and icon to the record
        if record.levelname in self.COLORS:
            color = self.COLORS[record.levelname]
            icon = self.ICONS.get(record.levelname, '')
            reset = self.COLORS['RESET']
            
            # Format the message with color and icon
            record.levelname_colored = f"{color}{icon} {record.levelname}{reset}"
            record.message_colored = f"{color}{record.getMessage()}{reset}"
        else:
            record.levelname_colored = record.levelname
            record.message_colored = record.getMessage()
        
        return super().format(record)


class TradingAgentsLogger:
    """
    Custom logger for TradingAgents with expert-specific file logging
    """
    
    def __init__(self, expert_id: Optional[int] = None, log_dir: str = None):
        """
        Initialize the logger
        
        Args:
            expert_id: Expert instance ID for log file naming
            log_dir: Directory to store log files (defaults to BA2 platform logs directory)
        """
        self.expert_id = expert_id
        
        # Use BA2 platform logs directory by default
        if log_dir is None:
            try:
                # Try to use BA2 platform's logs directory
                self.log_dir = os.path.join(ba2_config.HOME, "logs")
            except (AttributeError, ImportError):
                # Fallback to current directory if BA2 config not available
                self.log_dir = "."
        else:
            self.log_dir = log_dir
        
        # Create logger name
        logger_name = f"tradingagents_exp{expert_id}" if expert_id else "tradingagents"
        self.logger = logging.getLogger(logger_name)
        
        # Prevent duplicate handlers
        if self.logger.handlers:
            return
            
        self.logger.setLevel(logging.DEBUG)
        
        # Create formatters
        console_formatter = ColoredFormatter(
            '%(asctime)s - %(name)s - %(levelname_colored)s - %(message_colored)s'
        )
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        )
        
        # Console handler (respects STDOUT setting) with colors
        if self._should_log_to_console():
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(console_formatter)
            self.logger.addHandler(console_handler)
        
        # File handler (always enabled for file logging)
        if self._should_log_to_file():
            log_filename = f"tradeagents-exp{expert_id}.log" if expert_id else "tradeagents.log"
            log_filepath = os.path.join(self.log_dir, log_filename)
            
            # Create directory if it doesn't exist
            os.makedirs(self.log_dir, exist_ok=True)
            
            file_handler = RotatingFileHandler(
                log_filepath,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(file_formatter)
            self.logger.addHandler(file_handler)
    
    def _should_log_to_console(self) -> bool:
        """Check if console logging is enabled"""
        try:
            return getattr(ba2_config, 'STDOUT_LOGGING', True)
        except:
            # Fallback to environment variable
            console_logging = os.getenv("TRADINGAGENTS_CONSOLE_LOGGING", "true").lower()
            return console_logging in ("true", "1", "yes", "on")
    
    def _should_log_to_file(self) -> bool:
        """Check if file logging is enabled"""
        try:
            return getattr(ba2_config, 'FILE_LOGGING', True)
        except:
            # Fallback to environment variable
            file_logging = os.getenv("TRADINGAGENTS_FILE_LOGGING", "true").lower()
            return file_logging in ("true", "1", "yes", "on")
    
    def debug(self, message: str, *args, **kwargs):
        """Log debug message"""
        self.logger.debug(message, *args, **kwargs)
    
    def info(self, message: str, *args, **kwargs):
        """Log info message"""
        self.logger.info(message, *args, **kwargs)
    
    def warning(self, message: str, *args, **kwargs):
        """Log warning message"""
        self.logger.warning(message, *args, **kwargs)
    
    def error(self, message: str, *args, **kwargs):
        """Log error message"""
        self.logger.error(message, *args, **kwargs)
    
    def critical(self, message: str, *args, **kwargs):
        """Log critical message"""
        self.logger.critical(message, *args, **kwargs)
    
    def log_tool_call(self, tool_name: str, inputs: dict, agent_type: str = "unknown"):
        """Log a tool call"""
        self.info(f"[TOOL] [{agent_type}] Calling tool: {tool_name} with inputs: {inputs}")
    
    def log_tool_result(self, tool_name: str, success: bool, result_summary: str = ""):
        """Log tool call result"""
        if success:
            self.info(f"[SUCCESS] Tool {tool_name}: {result_summary}")
        else:
            self.error(f"[FAILED] Tool {tool_name}: {result_summary}")
    
    def log_agent_start(self, agent_type: str, symbol: str):
        """Log agent starting analysis"""
        icon_map = {
            "market": "[MARKET]",
            "news": "[NEWS]", 
            "fundamentals": "[FUND]",
            "social": "[SOCIAL]",
            "macro": "[MACRO]",
            "bull": "[BULL]",
            "bear": "[BEAR]",
            "trader": "[TRADER]",
            "manager": "[MGR]"
        }
        
        # Try Unicode icons first
        try:
            unicode_icons = {
                "market": "📈",
                "news": "📰", 
                "fundamentals": "🏢",
                "social": "💬",
                "macro": "🌍",
                "bull": "🐂",
                "bear": "🐻",
                "trader": "💼",
                "manager": "👔"
            }
            icon = unicode_icons.get(agent_type.lower(), "🤖")
            # Test if Unicode can be encoded
            icon.encode(sys.stdout.encoding or 'utf-8')
        except (UnicodeEncodeError, AttributeError):
            # Fallback to ASCII
            icon = icon_map.get(agent_type.lower(), "[AGENT]")
            
        self.info(f"{icon} [{agent_type}] Starting analysis for {symbol}")
    
    def log_agent_complete(self, agent_type: str, symbol: str, success: bool):
        """Log agent completion"""
        icon_map = {
            "market": "[MARKET]",
            "news": "[NEWS]", 
            "fundamentals": "[FUND]",
            "social": "[SOCIAL]",
            "macro": "[MACRO]",
            "bull": "[BULL]",
            "bear": "[BEAR]",
            "trader": "[TRADER]",
            "manager": "[MGR]"
        }
        
        # Try Unicode icons first
        try:
            unicode_icons = {
                "market": "📈",
                "news": "📰", 
                "fundamentals": "🏢",
                "social": "💬",
                "macro": "🌍",
                "bull": "🐂",
                "bear": "🐻",
                "trader": "💼",
                "manager": "👔"
            }
            icon = unicode_icons.get(agent_type.lower(), "🤖")
            status_icon = "✅" if success else "❌"
            # Test if Unicode can be encoded
            icon.encode(sys.stdout.encoding or 'utf-8')
            status_icon.encode(sys.stdout.encoding or 'utf-8')
        except (UnicodeEncodeError, AttributeError):
            # Fallback to ASCII
            icon = icon_map.get(agent_type.lower(), "[AGENT]")
            status_icon = "[OK]" if success else "[FAIL]"
            
        status = "completed successfully" if success else "failed"
        self.info(f"{icon} [{agent_type}] Analysis for {symbol} {status} {status_icon}")
    
    def log_step_start(self, step_name: str, context: str = ""):
        """Log step starting"""
        context_str = f" ({context})" if context else ""
        try:
            "🚀".encode(sys.stdout.encoding or 'utf-8')
            self.info(f"🚀 Starting step: {step_name}{context_str}")
        except (UnicodeEncodeError, AttributeError):
            self.info(f"[START] Starting step: {step_name}{context_str}")
    
    def log_step_complete(self, step_name: str, success: bool, duration: float = None):
        """Log step completion"""
        status = "completed" if success else "failed"
        duration_str = f" in {duration:.2f}s" if duration else ""
        
        try:
            status_icon = "✅" if success else "❌"
            status_icon.encode(sys.stdout.encoding or 'utf-8')
            self.info(f"{status_icon} Step {step_name} {status}{duration_str}")
        except (UnicodeEncodeError, AttributeError):
            status_icon = "[OK]" if success else "[FAIL]"
            self.info(f"{status_icon} Step {step_name} {status}{duration_str}")


# Global logger instance
_global_logger: Optional[TradingAgentsLogger] = None


def get_logger(expert_id: Optional[int] = None, log_dir: str = None) -> TradingAgentsLogger:
    """
    Get or create a logger instance
    
    Args:
        expert_id: Expert instance ID for log file naming
        log_dir: Directory to store log files
        
    Returns:
        TradingAgentsLogger instance
    """
    global _global_logger
    
    # Create new logger if needed or if parameters changed
    if (_global_logger is None or 
        _global_logger.expert_id != expert_id or 
        _global_logger.log_dir != log_dir):
        _global_logger = TradingAgentsLogger(expert_id, log_dir)
    
    return _global_logger


def init_logger(expert_id: Optional[int] = None, log_dir: str = None):
    """
    Initialize the global logger
    
    Args:
        expert_id: Expert instance ID for log file naming
        log_dir: Directory to store log files
    """
    global _global_logger
    _global_logger = TradingAgentsLogger(expert_id, log_dir)


# Convenience functions that use the global logger
def debug(message: str, *args, **kwargs):
    """Log debug message using global logger"""
    if _global_logger:
        _global_logger.debug(message, *args, **kwargs)


def info(message: str, *args, **kwargs):
    """Log info message using global logger"""
    if _global_logger:
        _global_logger.info(message, *args, **kwargs)


def warning(message: str, *args, **kwargs):
    """Log warning message using global logger"""
    if _global_logger:
        _global_logger.warning(message, *args, **kwargs)


def error(message: str, *args, **kwargs):
    """Log error message using global logger"""
    if _global_logger:
        _global_logger.error(message, *args, **kwargs)


def critical(message: str, *args, **kwargs):
    """Log critical message using global logger"""
    if _global_logger:
        _global_logger.critical(message, *args, **kwargs)


def log_tool_call(tool_name: str, inputs: dict, agent_type: str = "unknown"):
    """Log a tool call using global logger"""
    if _global_logger:
        _global_logger.log_tool_call(tool_name, inputs, agent_type)


def log_tool_result(tool_name: str, success: bool, result_summary: str = ""):
    """Log tool call result using global logger"""
    if _global_logger:
        _global_logger.log_tool_result(tool_name, success, result_summary)


def log_agent_start(agent_type: str, symbol: str):
    """Log agent starting analysis using global logger"""
    if _global_logger:
        _global_logger.log_agent_start(agent_type, symbol)


def log_agent_complete(agent_type: str, symbol: str, success: bool):
    """Log agent completion using global logger"""
    if _global_logger:
        _global_logger.log_agent_complete(agent_type, symbol, success)


def log_step_start(step_name: str, context: str = ""):
    """Log step starting using global logger"""
    if _global_logger:
        _global_logger.log_step_start(step_name, context)


def log_step_complete(step_name: str, success: bool, duration: float = None):
    """Log step completion using global logger"""
    if _global_logger:
        _global_logger.log_step_complete(step_name, success, duration)