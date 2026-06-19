import os
from typing import Optional
HOME = os.path.abspath(os.path.join(os.path.dirname(__file__)))
HOME_PARENT = os.path.abspath(os.path.join(HOME, ".."))

# ---------------------------------------------------------------------------
# BA2 data root layout (NOTHING is cached inside the code repos).
#
# A single root BA2_HOME (env-overridable, default ~/Documents/ba2) holds three
# buckets, split by OWNERSHIP — shared CACHE in common, per-platform DATA in its bucket:
#   common/  shared raw provider CACHE only (OHLCV parquet, fmp_history, screener,
#            options) — all under common/cache; used by both test + live.
#   test/    BA2TestPlatform DATA: its single test DB (dl_forecasting.db = app data
#            + appsetting keys), datasets, trained_models, job/news caches, exports.
#   trade/   live trade-platform DATA: its instance DB(s).
#
# Per-path env overrides (DB_FILE, CACHE_FOLDER, ...) still win for backward compat.
# These are plain module-level paths only — no DB calls at import time.
# ---------------------------------------------------------------------------
BA2_HOME = os.path.abspath(
    os.getenv("BA2_HOME", os.path.join(os.path.expanduser("~"), "Documents", "ba2"))
)
COMMON_DIR = os.path.join(BA2_HOME, "common")
TEST_DIR = os.path.join(BA2_HOME, "test")
TRADE_DIR = os.path.join(BA2_HOME, "trade")

# Default paths - can be overridden via command-line arguments / env.
# Logs go under the data layout (NOT the repo): the test platform's import-time logs land in
# test/logs; per-instance runs then relocate to <db folder>/logs via configure_db (so the live
# platform's logs follow its trade DB). Env-overridable.
LOG_FOLDER = os.getenv("LOG_FOLDER", os.path.join(TEST_DIR, 'logs'))
# The DB path is a PER-PLATFORM setting, NOT a shared-package default — ba2_common (shared) must
# not bake in test- or trade-specific paths. Each platform configures its OWN DB at startup via
# ba2_common.core.db.configure_db(): the LIVE platform -> trade/db.sqlite (ba2_trade_platform
# .config + seam_wiring); the TEST platform -> its single app DB test/dl_forecasting.db
# (app/models/database.py DATABASE_URL, wired in app.main). This value is only a last-resort
# fallback if neither configured one. Env override (DB_FILE) still wins.
DB_FILE = os.getenv("DB_FILE", os.path.join(BA2_HOME, "db.sqlite"))
# Shared raw provider cache (OHLCV parquet, fmp_history, as_of provider cache).
CACHE_FOLDER = os.getenv("CACHE_FOLDER", os.path.join(COMMON_DIR, "cache"))

# Every on-disk CACHE is a sub-path of CACHE_FOLDER (the one cache folder) — alongside the
# OHLCV parquet (<CACHE_FOLDER>/<provider>/) and fmp_history — so the whole cache relocates
# together when CACHE_FOLDER / BA2_HOME is overridden. Non-cache data stays in its bucket
# (test/ datasets+models, trade/ live DBs, common/ keys DB). Callers/CLI flags may override.
SCREENER_STORE_DIR = os.path.join(CACHE_FOLDER, "screener", "metric_store")
SCREENER_HISTORY_DB = os.path.join(CACHE_FOLDER, "screener", "screener_history.sqlite")
OPTIONS_CACHE_DB = os.path.join(CACHE_FOLDER, "options", "options_history.sqlite")

# Default HTTP port for the web interface
HTTP_PORT = 8080

# Storage secret for NiceGUI session storage (app.storage.user)
# Used for persisting user preferences like account filter selection
STORAGE_SECRET = 'ba2_trade_platform_default_secret'

#https://alpaca.markets/learn/connect-to-alpaca-api

# Logging sinks. Default on, but env-overridable (set BA2_STDOUT_LOGGING / BA2_FILE_LOGGING
# to "0") so multiprocessing workers can disable the RotatingFileHandler — multiple processes
# sharing one app.log race on rollover (Windows WinError 32). Must be read BEFORE the logger
# module is imported in such a worker.
STDOUT_LOGGING = os.getenv("BA2_STDOUT_LOGGING", "1") != "0"
FILE_LOGGING = os.getenv("BA2_FILE_LOGGING", "1") != "0"
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
        from ba2_common.core.db import get_db
        from ba2_common.core.models import AppSetting
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
