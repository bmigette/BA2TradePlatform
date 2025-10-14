
import os
from dotenv import load_dotenv
from typing import Optional
HOME = os.path.abspath(os.path.join(os.path.dirname(__file__))) 
HOME_PARENT = os.path.abspath(os.path.join(HOME, "..")) 
LOG_FOLDER = os.path.join(HOME_PARENT, 'logs')
#https://alpaca.markets/learn/connect-to-alpaca-api

STDOUT_LOGGING = True
FILE_LOGGING = True
FINNHUB_API_KEY=None
OPENAI_API_KEY=None
OPENAI_BACKEND_URL="https://api.openai.com/v1"  # Default OpenAI API endpoint
OPENAI_MODEL="gpt-5"  # Default OpenAI model for AI instrument selection
OPENAI_FALLBACK_MODEL="gpt-3.5-turbo"  # Fallback model if primary model fails
ALPHA_VANTAGE_API_KEY=None

DB_FILE = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "db.sqlite")
CACHE_FOLDER = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "cache")
# Price cache duration in seconds
PRICE_CACHE_TIME = 60  # Default to 60 seconds

def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, OPENAI_BACKEND_URL, OPENAI_MODEL, OPENAI_FALLBACK_MODEL, ALPHA_VANTAGE_API_KEY, FILE_LOGGING, PRICE_CACHE_TIME
    """Loads configuration from environment variables and database app settings."""

    env_file = os.path.join(HOME_PARENT, '.env')
    load_dotenv(env_file)
    FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', FINNHUB_API_KEY)
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', OPENAI_API_KEY)
    OPENAI_BACKEND_URL = os.getenv('OPENAI_BACKEND_URL', OPENAI_BACKEND_URL)
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', OPENAI_MODEL)
    OPENAI_FALLBACK_MODEL = os.getenv('OPENAI_FALLBACK_MODEL', OPENAI_FALLBACK_MODEL)
    ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', ALPHA_VANTAGE_API_KEY)
    
    # Load price cache time from environment, default to 30 seconds
    try:
        PRICE_CACHE_TIME = int(os.getenv('PRICE_CACHE_TIME', PRICE_CACHE_TIME))
    except ValueError:
        PRICE_CACHE_TIME = 60
    
    # Override with database app settings if available
    try:
        db_openai_key = get_app_setting('openai_api_key')
        if db_openai_key:
            OPENAI_API_KEY = db_openai_key
        
        db_openai_model = get_app_setting('openai_quick_think_llm')
        if db_openai_model:
            OPENAI_MODEL = db_openai_model
    except Exception:
        # Database might not be initialized yet, ignore errors
        pass    


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


def set_app_setting(key: str, value: str) -> None:
    """
    Set an application setting in the database.
    
    Creates a new setting if it doesn't exist, updates if it does.
    
    Args:
        key: Setting key
        value: Setting value
    
    Example:
        >>> set_app_setting("alpaca_market_api_key", "PKxxxx")
        >>> set_app_setting("alpaca_market_api_secret", "xxxx")
    """
    from ba2_trade_platform.core.db import get_db
    from ba2_trade_platform.core.models import AppSetting
    from sqlmodel import Session, select
    
    engine = get_db()
    with Session(engine.bind) as session:
        statement = select(AppSetting).where(AppSetting.key == key)
        setting = session.exec(statement).first()
        
        if setting:
            # Update existing
            setting.value_str = value
            session.add(setting)
        else:
            # Create new
            new_setting = AppSetting(key=key, value_str=value)
            session.add(new_setting)
        
        session.commit()
