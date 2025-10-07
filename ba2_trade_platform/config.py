
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
# Price cache duration in seconds
PRICE_CACHE_TIME = 30  # Default to 30 seconds

def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, ALPHA_VANTAGE_API_KEY, FILE_LOGGING, PRICE_CACHE_TIME
    """Loads configuration from environment variables."""

    env_file = os.path.join(HOME_PARENT, '.env')
    load_dotenv(env_file)
    FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', FINNHUB_API_KEY)
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', OPENAI_API_KEY)
    ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', ALPHA_VANTAGE_API_KEY)
    
    # Load price cache time from environment, default to 30 seconds
    try:
        PRICE_CACHE_TIME = int(os.getenv('PRICE_CACHE_TIME', PRICE_CACHE_TIME))
    except ValueError:
        PRICE_CACHE_TIME = 30    
