"""
TradingAgents Logger System
Provides expert-specific logging using BA2 platform's unified logger system
"""
import logging
from typing import Optional
from .... import logger as ba2_logger


# Global logger instance
_global_logger: Optional[logging.Logger] = None


def get_logger(expert_id: Optional[int] = None, log_dir: str = None) -> logging.Logger:
    """
    Get or create a logger instance using BA2 platform's unified system.
    
    Args:
        expert_id: Expert instance ID for log file naming
        log_dir: Directory to store log files (ignored, uses BA2 config)
        
    Returns:
        logging.Logger instance configured for TradingAgents expert
    """
    global _global_logger
    
    if _global_logger is None or (expert_id and not _global_logger.name.endswith(f"exp{expert_id}")):
        if expert_id:
            _global_logger = ba2_logger.get_expert_logger("TradingAgents", expert_id)
        else:
            # Fallback to main BA2 logger if no expert_id provided
            _global_logger = ba2_logger.logger
    
    return _global_logger


def init_logger(expert_id: Optional[int] = None, log_dir: str = None):
    """
    Initialize the global logger using BA2 platform's unified system.
    
    Args:
        expert_id: Expert instance ID for log file naming
        log_dir: Directory to store log files (ignored, uses BA2 config)
    """
    global _global_logger
    
    if expert_id:
        _global_logger = ba2_logger.get_expert_logger("TradingAgents", expert_id)
    else:
        _global_logger = ba2_logger.logger


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
    """
    Log error message using global logger.
    Note: exc_info should only be passed as True when inside an exception handler.
    """
    if _global_logger:
        _global_logger.error(message, *args, **kwargs)


def critical(message: str, *args, **kwargs):
    """Log critical message using global logger"""
    if _global_logger:
        _global_logger.critical(message, *args, **kwargs)


# Legacy helper functions for backwards compatibility
def log_tool_call(tool_name: str, inputs: dict, agent_type: str = "unknown"):
    """Log a tool call using global logger"""
    if _global_logger:
        _global_logger.info(f"[TOOL] [{agent_type}] Calling tool: {tool_name} with inputs: {inputs}")


def log_tool_result(tool_name: str, success: bool, result_summary: str = ""):
    """Log tool call result using global logger"""
    if _global_logger:
        if success:
            _global_logger.info(f"[SUCCESS] Tool {tool_name}: {result_summary}")
        else:
            _global_logger.error(f"[FAILED] Tool {tool_name}: {result_summary}")


def log_agent_start(agent_type: str, symbol: str):
    """Log agent starting analysis using global logger"""
    if _global_logger:
        _global_logger.info(f"[{agent_type.upper()}] Starting analysis for {symbol}")


def log_agent_complete(agent_type: str, symbol: str, success: bool):
    """Log agent completion using global logger"""
    if _global_logger:
        status = "completed successfully" if success else "failed"
        _global_logger.info(f"[{agent_type.upper()}] Analysis for {symbol} {status}")


def log_step_start(step_name: str, context: str = ""):
    """Log step starting using global logger"""
    if _global_logger:
        context_str = f" ({context})" if context else ""
        _global_logger.info(f"[START] Starting step: {step_name}{context_str}")


def log_step_complete(step_name: str, success: bool, duration: float = None):
    """Log step completion using global logger"""
    if _global_logger:
        status = "completed" if success else "failed"
        duration_str = f" in {duration:.2f}s" if duration else ""
        _global_logger.info(f"[{'OK' if success else 'FAIL'}] Step {step_name} {status}{duration_str}")