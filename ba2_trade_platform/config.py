
import os
from dotenv import load_dotenv
HOME = os.path.abspath(os.path.join(os.path.dirname(__file__))) 
HOME_PARENT = os.path.abspath(os.path.join(HOME, "..")) 
LOG_FOLDER = os.path.join(HOME_PARENT, 'logs')
#https://alpaca.markets/learn/connect-to-alpaca-api

STDOUT_LOGGING = True
FILE_LOGGING = True
FINNHUB_API_KEY=None
OPENAI_API_KEY=None
ALPHA_VANTAGE_API_KEY=None

DB_FILE = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "db.sqlite")
CACHE_FOLDER = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "cache")
# Default account refresh interval in minutes
account_refresh_interval = 60  # Default to 60 minutes

def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, ALPHA_VANTAGE_API_KEY, FILE_LOGGING, account_refresh_interval
    """Loads configuration from environment variables."""

    env_file = os.path.join(HOME_PARENT, '.env')
    load_dotenv(env_file)
    FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', FINNHUB_API_KEY)
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', OPENAI_API_KEY)
    ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', ALPHA_VANTAGE_API_KEY)
    
    # Load account refresh interval from environment, default to 60 minutes
    try:
        account_refresh_interval = int(os.getenv('ACCOUNT_REFRESH_INTERVAL', account_refresh_interval))
    except ValueError:
        account_refresh_interval = 60    
