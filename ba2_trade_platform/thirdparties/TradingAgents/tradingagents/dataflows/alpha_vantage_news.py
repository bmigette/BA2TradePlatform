from datetime import datetime
from dateutil.relativedelta import relativedelta
from .alpha_vantage_common import _make_api_request, format_datetime_for_api
from .config import get_config

def get_news(ticker: str, start_date: str = None, end_date: str = None) -> dict[str, str] | str:
    """Returns live and historical market news & sentiment data from premier news outlets worldwide.

    Covers stocks, cryptocurrencies, forex, and topics like fiscal policy, mergers & acquisitions, IPOs.

    Args:
        ticker: Stock symbol for news articles.
        start_date: Start date for news search. If not provided, defaults to news_lookback_days ago.
        end_date: End date for news search. If not provided, defaults to today.

    Returns:
        Dictionary containing news sentiment data or JSON string.
    """
    # Get config for defaults
    config = get_config()
    
    # Handle end_date default
    if end_date is None:
        end_dt = datetime.now()
        end_date = end_dt.strftime("%Y-%m-%d")
    else:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    # Handle start_date default
    if start_date is None:
        lookback_days = config.get('news_lookback_days', 7)
        start_dt = end_dt - relativedelta(days=lookback_days)
        start_date = start_dt.strftime("%Y-%m-%d")

    params = {
        "tickers": ticker,
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(end_date),
        "sort": "LATEST",
        "limit": "50",
    }
    
    return _make_api_request("NEWS_SENTIMENT", params)

def get_insider_transactions(symbol: str) -> dict[str, str] | str:
    """Returns latest and historical insider transactions by key stakeholders.

    Covers transactions by founders, executives, board members, etc.

    Args:
        symbol: Ticker symbol. Example: "IBM".

    Returns:
        Dictionary containing insider transaction data or JSON string.
    """

    params = {
        "symbol": symbol,
    }

    return _make_api_request("INSIDER_TRANSACTIONS", params)
