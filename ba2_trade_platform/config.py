import os
from typing import Optional
HOME = os.path.abspath(os.path.join(os.path.dirname(__file__))) 
HOME_PARENT = os.path.abspath(os.path.join(HOME, "..")) 

# Default paths - can be overridden via command-line arguments
LOG_FOLDER = os.path.join(HOME_PARENT, 'logs')
DB_FILE = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "db.sqlite")
CACHE_FOLDER = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "cache")

# Default HTTP port for the web interface
HTTP_PORT = 8080

# Storage secret for NiceGUI session storage (app.storage.user)
# Used for persisting user preferences like account filter selection
STORAGE_SECRET = 'ba2_trade_platform_default_secret'

#https://alpaca.markets/learn/connect-to-alpaca-api

STDOUT_LOGGING = True
FILE_LOGGING = True
OPENAI_BACKEND_URL="https://api.openai.com/v1"  # Default OpenAI API endpoint

# Price cache duration in seconds
PRICE_CACHE_TIME = 60  # Default to 60 seconds

# Database performance logging threshold in milliseconds
# Only log DB operations (queries, lock waits) exceeding this threshold
# Set DB_PERF_LOG_THRESHOLD_MS in .env to override
DB_PERF_LOG_THRESHOLD_MS = 100

# OpenAI streaming configuration
# Enable streaming responses from OpenAI API for faster initial response times
# When enabled, responses are sent incrementally as they're generated
# This can reduce perceived latency but may increase API costs slightly
# This can also reduce likelihood of timeouts for long responses
# Set OPENAI_ENABLE_STREAMING=false in .env file to disable
# See: https://platform.openai.com/docs/guides/streaming-responses
OPENAI_ENABLE_STREAMING = True  # Default to True for better performance

def get_app_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Get an application setting from the database.
    
    Args:
        key: Setting key to retrieve
        default: Default value if setting not found
    
    Returns:
        Setting value_str or default if not found
    
    Example:
        >>> api_key = get_app_setting("alpaca_market_api_key")
        >>> if not api_key:
        ...     raise ValueError("API key not configured")
    """
    try:
        from ba2_trade_platform.core.db import get_db
        from ba2_trade_platform.core.models import AppSetting
        from sqlmodel import Session, select
        
        engine = get_db()
        with Session(engine.bind) as session:
            statement = select(AppSetting).where(AppSetting.key == key)
            setting = session.exec(statement).first()
            
            if setting:
                return setting.value_str
            return default
            
    except Exception as e:
        # During initialization, database might not be ready yet
        # Fall back to default
        return default


def get_min_tp_sl_percent() -> float:
    """
    Get the minimum TP/SL percent setting from the database.
    
    This setting ensures that even when TP/SL is calculated from current market price,
    if prices slip overnight, the TP/SL will be enforced to maintain at least this
    percent above the order's open price.
    
    Returns:
        float: Minimum TP/SL percent (default 3.0 if not configured)
    
    Example:
        >>> min_percent = get_min_tp_sl_percent()  # Returns 3.0 or configured value
        >>> print(f"Minimum TP/SL: {min_percent}%")
    """
    try:
        value_str = get_app_setting('min_tp_sl_percent')
        if value_str:
            return float(value_str)
    except (ValueError, TypeError):
        pass
    
    # Default to 3.0 if not set or invalid
    return 3.0
