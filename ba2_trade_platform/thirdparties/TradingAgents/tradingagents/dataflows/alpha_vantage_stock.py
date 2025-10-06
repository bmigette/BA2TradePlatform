from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from .alpha_vantage_common import _make_api_request, _filter_csv_by_date_range
from .config import get_config

def get_stock(
    symbol: str,
    start_date: str = None,
    end_date: str = None
) -> str:
    """
    Returns raw daily OHLCV values, adjusted close values, and historical split/dividend events
    filtered to the specified date range.

    Args:
        symbol: The name of the equity. For example: symbol=IBM
        start_date: Start date in yyyy-mm-dd format. If not provided, defaults to market_history_days ago.
        end_date: End date in yyyy-mm-dd format. If not provided, defaults to today.

    Returns:
        CSV string containing the daily adjusted time series data filtered to the date range.
    """
    # Get config for defaults
    config = get_config()
    
    # Handle end_date default
    if end_date is None:
        today = datetime.now()
        end_date = today.strftime("%Y-%m-%d")
    else:
        today = datetime.strptime(end_date, "%Y-%m-%d")
    
    # Handle start_date default
    if start_date is None:
        lookback_days = config.get('market_history_days', 90)
        start_dt = today - relativedelta(days=lookback_days)
        start_date = start_dt.strftime("%Y-%m-%d")
    else:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    
    # Parse dates to determine the range
    days_from_today_to_start = (today - start_dt).days
    outputsize = "compact" if days_from_today_to_start < 100 else "full"

    params = {
        "symbol": symbol,
        "outputsize": outputsize,
        "datatype": "csv",
    }

    response = _make_api_request("TIME_SERIES_DAILY_ADJUSTED", params)

    return _filter_csv_by_date_range(response, start_date, end_date)
