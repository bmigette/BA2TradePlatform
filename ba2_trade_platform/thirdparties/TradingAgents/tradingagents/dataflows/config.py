from .. import default_config
from typing import Dict, Optional
import os

# Use default config but allow it to be overridden
_config: Optional[Dict] = None
DATA_DIR: Optional[str] = None


def get_api_key_from_database(key_name: str) -> Optional[str]:
    """Get API key from the BA2 Trade Platform database AppSetting table."""
    try:
        # Import here to avoid circular imports
        from ba2_trade_platform.core.db import get_setting
        return get_setting(key_name)
    except Exception as e:
        # Fallback to environment variable if database access fails
        print(f"Warning: Could not retrieve {key_name} from database, falling back to environment variable: {e}")
        return os.getenv(key_name.upper())


def get_openai_api_key() -> Optional[str]:
    """Get OpenAI API key from database or environment."""
    return get_api_key_from_database("openai_api_key")


def get_finnhub_api_key() -> Optional[str]:
    """Get Finnhub API key from database or environment."""
    return get_api_key_from_database("finnhub_api_key")


def get_fred_api_key() -> Optional[str]:
    """Get FRED API key from database or environment."""
    return get_api_key_from_database("fred_api_key")


def get_api_key(key_name: str, env_fallback: str) -> Optional[str]:
    """Generic API key getter with database fallback to environment."""
    return get_api_key_from_database(key_name) or os.getenv(env_fallback)


def set_environment_variables_from_database():
    """Set environment variables for API keys from database values for compatibility."""
    try:
        openai_key = get_openai_api_key()
        if openai_key:
            os.environ["OPENAI_API_KEY"] = openai_key
            
        finnhub_key = get_finnhub_api_key()
        if finnhub_key:
            os.environ["FINNHUB_API_KEY"] = finnhub_key
            
        fred_key = get_fred_api_key()
        if fred_key:
            os.environ["FRED_API_KEY"] = fred_key
            
        #print("API keys loaded from database and set as environment variables")
    except Exception as e:
        print(f"Warning: Could not set environment variables from database: {e}")


def initialize_config():
    """Initialize the configuration with default values."""
    global _config, DATA_DIR
    if _config is None:
        _config = default_config.DEFAULT_CONFIG.copy()
        DATA_DIR = _config["data_dir"]


def set_config(config: Dict):
    """Update the configuration with custom values."""
    global _config, DATA_DIR
    if _config is None:
        _config = default_config.DEFAULT_CONFIG.copy()
    _config.update(config)
    DATA_DIR = _config["data_dir"]


def get_config() -> Dict:
    """Get the current configuration."""
    if _config is None:
        initialize_config()
    
    # Add logging configuration from main config
    config = _config.copy()
    try:
        from ba2_trade_platform.config import get_env_var
        config['console_logging'] = get_env_var('STDOUT_LOGGING', 'true').lower() in ('true', '1', 'yes', 'on')
        config['file_logging'] = get_env_var('FILE_LOGGING', 'true').lower() in ('true', '1', 'yes', 'on')
    except ImportError:
        # Fallback to environment variables
        config['console_logging'] = os.getenv('STDOUT_LOGGING', 'true').lower() in ('true', '1', 'yes', 'on')
        config['file_logging'] = os.getenv('FILE_LOGGING', 'true').lower() in ('true', '1', 'yes', 'on')
    
    return config


# Initialize with default config
initialize_config()
