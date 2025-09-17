
import os
from dotenv import load_dotenv
HOME = os.path.abspath(os.path.join(os.path.dirname(__file__))) 
HOME_PARENT = os.path.abspath(os.path.join(HOME, "..")) 
#https://alpaca.markets/learn/connect-to-alpaca-api

STDOUT_LOGGING = True
FILE_LOGGING = True
FINNHUB_API_KEY=None
OPENAI_API_KEY=None
APCA_API_BASE_URL=None
APCA_API_KEY_ID=None
APCA_API_SECRET_KEY=None
DB_FILE = os.path.join(os.path.expanduser("~"), "Documents", "ba2_trade_platform", "db.sqlite")

def load_config_from_env() -> None:
    global FINNHUB_API_KEY, OPENAI_API_KEY, FILE_LOGGING, APCA_API_BASE_URL, APCA_API_KEY_ID, APCA_API_SECRET_KEY
    """Loads configuration from environment variables."""

    env_file = os.path.join(HOME_PARENT, '.env')
    load_dotenv(env_file)
    FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY', FINNHUB_API_KEY)
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', OPENAI_API_KEY)    
    APCA_API_BASE_URL = os.getenv('APCA_API_BASE_URL', APCA_API_BASE_URL)
    APCA_API_KEY_ID = os.getenv('APCA_API_KEY_ID', APCA_API_KEY_ID)
    APCA_API_SECRET_KEY = os.getenv('APCA_API_SECRET_KEY', APCA_API_SECRET_KEY)