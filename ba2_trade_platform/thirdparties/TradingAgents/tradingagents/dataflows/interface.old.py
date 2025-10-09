from typing import Annotated, Dict
from .reddit_utils import fetch_top_from_category
from .yfin_utils import *
from .stockstats_utils import *
from dateutil.relativedelta import relativedelta
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import pandas as pd
from tqdm import tqdm
import yfinance as yf
from openai import OpenAI
from .config import get_config, set_config, DATA_DIR
from .. import logger
from ba2_trade_platform.modules.dataproviders import YFinanceDataProvider

# Import vendor-specific modules (for backward compatibility with direct imports)
from .y_finance import (
    get_YFin_data_online,
    get_stock_stats_indicators_window,
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions
)

# BA2 Provider Integration
from ba2_trade_platform.modules.dataproviders import get_provider
from ba2_trade_platform.modules.dataproviders.alpha_vantage_common import AlphaVantageRateLimitError
from ba2_trade_platform.core.ProviderWithPersistence import ProviderWithPersistence
from ba2_trade_platform.logger import logger as ba2_logger

# Mapping of TradingAgents (method, vendor) to BA2 (category, provider_name)
BA2_PROVIDER_MAP = {
    # OHLCV data providers
    ("get_stock_data", "alpha_vantage"): ("ohlcv", "alphavantage"),
    ("get_stock_data", "yfinance"): ("ohlcv", "yfinance"),
    
    # News providers
    ("get_news", "alpha_vantage"): ("news", "alphavantage"),
    ("get_news", "google"): ("news", "google"),
    ("get_news", "openai"): ("news", "openai"),
    ("get_global_news", "openai"): ("news", "openai"),
    
    # Indicators providers
    ("get_indicators", "alpha_vantage"): ("indicators", "alphavantage"),
    ("get_indicators", "yfinance"): ("indicators", "yfinance"),
    
    # Fundamentals overview providers (company overview, key metrics)
    ("get_fundamentals", "alpha_vantage"): ("fundamentals_overview", "alphavantage"),
    ("get_fundamentals", "openai"): ("fundamentals_overview", "openai"),
    
    # Fundamentals details providers (financial statements)
    ("get_balance_sheet", "alpha_vantage"): ("fundamentals_details", "alphavantage"),
    ("get_cashflow", "alpha_vantage"): ("fundamentals_details", "alphavantage"),
    ("get_income_statement", "alpha_vantage"): ("fundamentals_details", "alphavantage"),
    ("get_balance_sheet", "yfinance"): ("fundamentals_details", "yfinance"),
    ("get_cashflow", "yfinance"): ("fundamentals_details", "yfinance"),
    ("get_income_statement", "yfinance"): ("fundamentals_details", "yfinance"),
    
    # Macro providers
    ("get_economic_indicators", "fred"): ("macro", "fred"),
    ("get_yield_curve", "fred"): ("macro", "fred"),
    ("get_fed_calendar", "fred"): ("macro", "fred"),
}

# DISABLED - finnhub_utils file doesn't exist
# def get_finnhub_news(
#     ticker: Annotated[
#         str,
#         "Search query of a company's, e.g. 'AAPL, TSM, etc.",
#     ],
#     curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
#     look_back_days: Annotated[
#         int,
#         "Number of days to look back. If not provided, defaults to news_lookback_days from config (typically 7 days). You can specify a custom value to get more or less historical news."
#     ] = None,
# ):
#     """
#     Retrieve news about a company within a time frame
# 
#     Args
#         ticker (str): ticker for the company you are interested in
#         curr_date (str): Current date in yyyy-mm-dd format
#         look_back_days (int, optional): Number of days to look back. Defaults to config value if None.
#     Returns
#         str: dataframe containing the news of the company in the time frame
# 
#     """
#     # Get lookback days from config if not provided
#     if look_back_days is None:
#         config = get_config()
#         look_back_days = config.get('news_lookback_days', 7)
# 
#     start_date = datetime.strptime(curr_date, "%Y-%m-%d")
#     before = start_date - relativedelta(days=look_back_days)
#     before = before.strftime("%Y-%m-%d")
# 
#     result = get_data_in_range(ticker, before, curr_date, "news_data", DATA_DIR)
# 
#     if len(result) == 0:
#         return ""
# 
#     combined_result = ""
#     for day, data in result.items():
#         if len(data) == 0:
#             continue
#         for entry in data:
#             current_news = (
#                 "### " + entry["headline"] + f" ({day})" + "\n" + entry["summary"]
#             )
#             combined_result += current_news + "\n\n"
# 
#     return f"## {ticker} News, from {before} to {curr_date}:\n" + str(combined_result)


# DISABLED - finnhub_utils file doesn't exist
# def get_finnhub_company_insider_sentiment(
#     ticker: Annotated[str, "ticker symbol for the company"],
#     curr_date: Annotated[
#         str,
#         "current date of you are trading at, yyyy-mm-dd",
#     ],
#     look_back_days: Annotated[
#         int,
#         "Number of days to look back for insider sentiment data. If not provided, defaults to market_history_days from config (typically 90 days). You can specify a custom value to analyze shorter or longer periods."
#     ] = None,
# ):
#     """
#     Retrieve insider sentiment about a company (retrieved from public SEC information)
#     Args:
#         ticker (str): ticker symbol of the company
#         curr_date (str): current date you are trading on, yyyy-mm-dd
#         look_back_days (int, optional): Number of days to look back. Defaults to config value if None.
#     Returns:
#         str: a report of the insider sentiment starting at curr_date
#     """
#     # Get lookback days from config if not provided
#     if look_back_days is None:
#         config = get_config()
#         look_back_days = config.get('market_history_days', 90)
# 
#     date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
#     before = date_obj - relativedelta(days=look_back_days)
#     before = before.strftime("%Y-%m-%d")
# 
#     data = get_data_in_range(ticker, before, curr_date, "insider_senti", DATA_DIR)
# 
#     if len(data) == 0:
#         return ""
# 
#     result_str = ""
#     seen_dicts = []
#     for date, senti_list in data.items():
#         for entry in senti_list:
#             if entry not in seen_dicts:
#                 result_str += f"### {entry['year']}-{entry['month']}:\nChange: {entry['change']}\nMonthly Share Purchase Ratio: {entry['mspr']}\n\n"
#                 seen_dicts.append(entry)
# 
#     return (
#         f"## {ticker} Insider Sentiment Data for {before} to {curr_date}:\n"
#         + result_str
#         + "The change field refers to the net buying/selling from all insiders' transactions. The mspr field refers to monthly share purchase ratio."
#     )


# DISABLED - finnhub_utils file doesn't exist
# def get_finnhub_company_insider_transactions(
#     ticker: Annotated[str, "ticker symbol"],
#     curr_date: Annotated[
#         str,
#         "current date you are trading at, yyyy-mm-dd",
#     ],
#     look_back_days: Annotated[
#         int,
#         "Number of days to look back for insider transactions. If not provided, defaults to market_history_days from config (typically 90 days). You can specify a custom value to analyze shorter or longer periods."
#     ] = None,
# ):
#     """
#     Retrieve insider transaction information about a company (retrieved from public SEC information)
#     Args:
#         ticker (str): ticker symbol of the company
#         curr_date (str): current date you are trading at, yyyy-mm-dd
#         look_back_days (int, optional): Number of days to look back. Defaults to config value if None.
#     Returns:
#         str: a report of the company's insider transaction/trading information
#     """
#     # Get lookback days from config if not provided
#     if look_back_days is None:
#         config = get_config()
#         look_back_days = config.get('market_history_days', 90)
# 
#     date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
#     before = date_obj - relativedelta(days=look_back_days)
#     before = before.strftime("%Y-%m-%d")
# 
#     data = get_data_in_range(ticker, before, curr_date, "insider_trans", DATA_DIR)
# 
#     if len(data) == 0:
#         return ""
# 
#     result_str = ""
# 
#     seen_dicts = []
#     for date, senti_list in data.items():
#         for entry in senti_list:
#             if entry not in seen_dicts:
#                 result_str += f"### Filing Date: {entry['filingDate']}, {entry['name']}:\nChange:{entry['change']}\nShares: {entry['share']}\nTransaction Price: {entry['transactionPrice']}\nTransaction Code: {entry['transactionCode']}\n\n"
#                 seen_dicts.append(entry)
# 
#     return (
#         f"## {ticker} insider transactions from {before} to {curr_date}:\n"
#         + result_str
#         + "The change field reflects the variation in share count—here a negative number indicates a reduction in holdings—while share specifies the total number of shares involved. The transactionPrice denotes the per-share price at which the trade was executed, and transactionDate marks when the transaction occurred. The name field identifies the insider making the trade, and transactionCode (e.g., S for sale) clarifies the nature of the transaction. FilingDate records when the transaction was officially reported, and the unique id links to the specific SEC filing, as indicated by the source. Additionally, the symbol ties the transaction to a particular company, isDerivative flags whether the trade involves derivative securities, and currency notes the currency context of the transaction."
#     )


def get_simfin_balance_sheet(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[
        str,
        "reporting frequency of the company's financial history: annual / quarterly",
    ],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
):
    data_path = os.path.join(
        DATA_DIR,
        "fundamental_data",
        "simfin_data_all",
        "balance_sheet",
        "companies",
        "us",
        f"us-balance-{freq}.csv",
    )
    df = pd.read_csv(data_path, sep=";")

    # Convert date strings to datetime objects and remove any time components
    df["Report Date"] = pd.to_datetime(df["Report Date"], utc=True).dt.normalize()
    df["Publish Date"] = pd.to_datetime(df["Publish Date"], utc=True).dt.normalize()

    # Convert the current date to datetime and normalize
    curr_date_dt = pd.to_datetime(curr_date, utc=True).normalize()

    # Filter the DataFrame for the given ticker and for reports that were published on or before the current date
    filtered_df = df[(df["Ticker"] == ticker) & (df["Publish Date"] <= curr_date_dt)]

    # Check if there are any available reports; if not, return a notification
    if filtered_df.empty:
        print("No balance sheet available before the given current date.")
        return ""

    # Get the most recent balance sheet by selecting the row with the latest Publish Date
    latest_balance_sheet = filtered_df.loc[filtered_df["Publish Date"].idxmax()]

    # drop the SimFinID column
    latest_balance_sheet = latest_balance_sheet.drop("SimFinId")

    return (
        f"## {freq} balance sheet for {ticker} released on {str(latest_balance_sheet['Publish Date'])[0:10]}: \n"
        + str(latest_balance_sheet)
        + "\n\nThis includes metadata like reporting dates and currency, share details, and a breakdown of assets, liabilities, and equity. Assets are grouped as current (liquid items like cash and receivables) and noncurrent (long-term investments and property). Liabilities are split between short-term obligations and long-term debts, while equity reflects shareholder funds such as paid-in capital and retained earnings. Together, these components ensure that total assets equal the sum of liabilities and equity."
    )


def get_simfin_cashflow(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[
        str,
        "reporting frequency of the company's financial history: annual / quarterly",
    ],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
):
    data_path = os.path.join(
        DATA_DIR,
        "fundamental_data",
        "simfin_data_all",
        "cash_flow",
        "companies",
        "us",
        f"us-cashflow-{freq}.csv",
    )
    df = pd.read_csv(data_path, sep=";")

    # Convert date strings to datetime objects and remove any time components
    df["Report Date"] = pd.to_datetime(df["Report Date"], utc=True).dt.normalize()
    df["Publish Date"] = pd.to_datetime(df["Publish Date"], utc=True).dt.normalize()

    # Convert the current date to datetime and normalize
    curr_date_dt = pd.to_datetime(curr_date, utc=True).normalize()

    # Filter the DataFrame for the given ticker and for reports that were published on or before the current date
    filtered_df = df[(df["Ticker"] == ticker) & (df["Publish Date"] <= curr_date_dt)]

    # Check if there are any available reports; if not, return a notification
    if filtered_df.empty:
        print("No cash flow statement available before the given current date.")
        return ""

    # Get the most recent cash flow statement by selecting the row with the latest Publish Date
    latest_cash_flow = filtered_df.loc[filtered_df["Publish Date"].idxmax()]

    # drop the SimFinID column
    latest_cash_flow = latest_cash_flow.drop("SimFinId")

    return (
        f"## {freq} cash flow statement for {ticker} released on {str(latest_cash_flow['Publish Date'])[0:10]}: \n"
        + str(latest_cash_flow)
        + "\n\nThis includes metadata like reporting dates and currency, share details, and a breakdown of cash movements. Operating activities show cash generated from core business operations, including net income adjustments for non-cash items and working capital changes. Investing activities cover asset acquisitions/disposals and investments. Financing activities include debt transactions, equity issuances/repurchases, and dividend payments. The net change in cash represents the overall increase or decrease in the company's cash position during the reporting period."
    )


def get_simfin_income_statements(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[
        str,
        "reporting frequency of the company's financial history: annual / quarterly",
    ],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
):
    data_path = os.path.join(
        DATA_DIR,
        "fundamental_data",
        "simfin_data_all",
        "income_statements",
        "companies",
        "us",
        f"us-income-{freq}.csv",
    )
    df = pd.read_csv(data_path, sep=";")

    # Convert date strings to datetime objects and remove any time components
    df["Report Date"] = pd.to_datetime(df["Report Date"], utc=True).dt.normalize()
    df["Publish Date"] = pd.to_datetime(df["Publish Date"], utc=True).dt.normalize()

    # Convert the current date to datetime and normalize
    curr_date_dt = pd.to_datetime(curr_date, utc=True).normalize()

    # Filter the DataFrame for the given ticker and for reports that were published on or before the current date
    filtered_df = df[(df["Ticker"] == ticker) & (df["Publish Date"] <= curr_date_dt)]

    # Check if there are any available reports; if not, return a notification
    if filtered_df.empty:
        print("No income statement available before the given current date.")
        return ""

    # Get the most recent income statement by selecting the row with the latest Publish Date
    latest_income = filtered_df.loc[filtered_df["Publish Date"].idxmax()]

    # drop the SimFinID column
    latest_income = latest_income.drop("SimFinId")

    return (
        f"## {freq} income statement for {ticker} released on {str(latest_income['Publish Date'])[0:10]}: \n"
        + str(latest_income)
        + "\n\nThis includes metadata like reporting dates and currency, share details, and a comprehensive breakdown of the company's financial performance. Starting with Revenue, it shows Cost of Revenue and resulting Gross Profit. Operating Expenses are detailed, including SG&A, R&D, and Depreciation. The statement then shows Operating Income, followed by non-operating items and Interest Expense, leading to Pretax Income. After accounting for Income Tax and any Extraordinary items, it concludes with Net Income, representing the company's bottom-line profit or loss for the period."
    )


def get_reddit_global_news(
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    look_back_days: Annotated[
        int,
        "Number of days to look back for global news. If not provided, defaults to news_lookback_days from config (typically 7 days). You can specify a custom value to get more or less historical news."
    ] = None,
    max_limit_per_day: Annotated[int, "Maximum number of news per day"] = 5,
) -> str:
    """
    Retrieve the latest top reddit news
    Args:
        start_date: Start date in yyyy-mm-dd format
        look_back_days: Number of days to look back. Defaults to config value if None.
        max_limit_per_day: Maximum number of news per day
    Returns:
        str: A formatted dataframe containing the latest news articles posts on reddit and meta information in these columns: "created_utc", "id", "title", "selftext", "score", "num_comments", "url"
    """
    # Get lookback days from config if not provided
    if look_back_days is None:
        config = get_config()
        look_back_days = config.get('news_lookback_days', 7)

    start_date = datetime.strptime(start_date, "%Y-%m-%d")
    before = start_date - relativedelta(days=look_back_days)
    before = before.strftime("%Y-%m-%d")

    posts = []
    # iterate from start_date to end_date
    curr_date = datetime.strptime(before, "%Y-%m-%d")

    total_iterations = (start_date - curr_date).days + 1
    pbar = tqdm(desc=f"Getting Global News on {start_date}", total=total_iterations)

    while curr_date <= start_date:
        curr_date_str = curr_date.strftime("%Y-%m-%d")
        fetch_result = fetch_top_from_category(
            "global_news",
            curr_date_str,
            max_limit_per_day,
            data_path=os.path.join(DATA_DIR, "reddit_data"),
        )
        posts.extend(fetch_result)
        curr_date += relativedelta(days=1)
        pbar.update(1)

    pbar.close()

    if len(posts) == 0:
        return ""

    news_str = ""
    for post in posts:
        if post["content"] == "":
            news_str += f"### {post['title']}\n\n"
        else:
            news_str += f"### {post['title']}\n\n{post['content']}\n\n"

    return f"## Global News Reddit, from {before} to {curr_date}:\n{news_str}"


def get_reddit_company_news(
    ticker: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    look_back_days: Annotated[
        int,
        "Number of days to look back for company news. If not provided, defaults to news_lookback_days from config (typically 7 days). You can specify a custom value to get more or less historical news."
    ] = None,
    max_limit_per_day: Annotated[int, "Maximum number of news per day"] = 5,
) -> str:
    """
    Retrieve the latest top reddit news for a company
    Args:
        ticker: ticker symbol of the company
        start_date: Start date in yyyy-mm-dd format
        look_back_days: Number of days to look back. Defaults to config value if None.
        max_limit_per_day: Maximum number of news per day
    Returns:
        str: A formatted dataframe containing the latest news articles posts on reddit and meta information in these columns: "created_utc", "id", "title", "selftext", "score", "num_comments", "url"
    """
    # Get lookback days from config if not provided
    if look_back_days is None:
        config = get_config()
        look_back_days = config.get('news_lookback_days', 7)

    start_date = datetime.strptime(start_date, "%Y-%m-%d")
    before = start_date - relativedelta(days=look_back_days)
    before = before.strftime("%Y-%m-%d")

    posts = []
    # iterate from start_date to end_date
    curr_date = datetime.strptime(before, "%Y-%m-%d")

    total_iterations = (start_date - curr_date).days + 1
    pbar = tqdm(
        desc=f"Getting Company News for {ticker} on {start_date}",
        total=total_iterations,
    )

    while curr_date <= start_date:
        curr_date_str = curr_date.strftime("%Y-%m-%d")
        fetch_result = fetch_top_from_category(
            "company_news",
            curr_date_str,
            max_limit_per_day,
            ticker,
            data_path=os.path.join(DATA_DIR, "reddit_data"),
        )
        posts.extend(fetch_result)
        curr_date += relativedelta(days=1)

        pbar.update(1)

    pbar.close()

    if len(posts) == 0:
        return ""

    news_str = ""
    for post in posts:
        if post["content"] == "":
            news_str += f"### {post['title']}\n\n"
        else:
            news_str += f"### {post['title']}\n\n{post['content']}\n\n"

    return f"##{ticker} News Reddit, from {before} to {curr_date}:\n\n{news_str}"


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[
        int,
        "Number of days to look back for indicator data. If not provided, defaults to market_history_days from config (typically 90 days). You can specify a custom value to analyze shorter or longer periods."
    ] = None,
    online: Annotated[bool, "to fetch data online or offline"] = True,
    interval: Annotated[str, "Data interval (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1wk, 1mo)"] = None,
) -> str:
    """
    Get technical indicator values for a date range.
    Returns text format only - JSON format is created by db_storage.py for storage.
    """
    # Get lookback days from config if not provided
    if look_back_days is None:
        config = get_config()
        look_back_days = config.get('market_history_days', 90)
        
    # Get interval from config if not provided
    if interval is None:
        config = get_config()
        interval = config.get("timeframe", "1d")

    best_ind_params = {
        # Moving Averages
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance. "
            "Tips: It lags price; combine with faster indicators for timely signals."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups. "
            "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points. "
            "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        ),
        # MACD Related
        "macd": (
            "MACD: Computes momentum via differences of EMAs. "
            "Usage: Look for crossovers and divergence as signals of trend changes. "
            "Tips: Confirm with other indicators in low-volatility or sideways markets."
        ),
        "macds": (
            "MACD Signal: An EMA smoothing of the MACD line. "
            "Usage: Use crossovers with the MACD line to trigger trades. "
            "Tips: Should be part of a broader strategy to avoid false positives."
        ),
        "macdh": (
            "MACD Histogram: Shows the gap between the MACD line and its signal. "
            "Usage: Visualize momentum strength and spot divergence early. "
            "Tips: Can be volatile; complement with additional filters in fast-moving markets."
        ),
        # Momentum Indicators
        "rsi": (
            "RSI: Measures momentum to flag overbought/oversold conditions. "
            "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
            "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        ),
        # Volatility Indicators
        "boll": (
            "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
            "Usage: Acts as a dynamic benchmark for price movement. "
            "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
        ),
        "boll_ub": (
            "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
            "Usage: Signals potential overbought conditions and breakout zones. "
            "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
        ),
        "boll_lb": (
            "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
            "Usage: Indicates potential oversold conditions. "
            "Tips: Use additional analysis to avoid false reversal signals."
        ),
        "atr": (
            "ATR: Averages true range to measure volatility. "
            "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
            "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
        ),
        # Volume-Based Indicators
        "vwma": (
            "VWMA: A moving average weighted by volume. "
            "Usage: Confirm trends by integrating price action with volume data. "
            "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
        ),
        "mfi": (
            "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
            "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
            "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        ),
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )

    # Calculate date range
    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date_dt = curr_date_dt - relativedelta(days=look_back_days)
    start_date = start_date_dt.strftime("%Y-%m-%d")

    # Get indicator data for entire range efficiently
    try:
        indicator_df = StockstatsUtils.get_stock_stats_range(
            symbol=symbol,
            indicator=indicator,
            start_date=start_date,
            end_date=end_date,
            data_dir=os.path.join(DATA_DIR, "market_data", "price_data"),
            online=online,
            interval=interval,
        )
    except Exception as e:
        logger.error(f"Error getting indicator {indicator} for {symbol}: {e}", exc_info=True)
        return f"Error calculating {indicator}: {str(e)}"
    
    # Build text format for LangGraph agent
    ind_string = ""
    for _, row in indicator_df.iterrows():
        date_str = row["Date"]
        value = row["value"]
        
        # Handle N/A values
        if pd.isna(value) or (isinstance(value, str) and "N/A" in value):
            ind_string += f"{date_str}: N/A: Not a trading day (weekend or holiday)\n"
        else:
            ind_string += f"{date_str}: {value}\n"
    
    result_str = (
        f"## {indicator} values from {start_date} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + best_ind_params.get(indicator, "No description available.")
    )
    
    # Store only the PARAMETERS needed to reconstruct this indicator from cache
    # The UI will use YFinanceDataProvider + StockstatsUtils to recalculate
    json_data = {
        "tool": "get_stock_stats_indicators_window",
        "indicator": indicator,
        "symbol": symbol,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
        "look_back_days": look_back_days,
        "description": best_ind_params.get(indicator, "No description available."),
        "data_points": len(indicator_df)  # Just the count, not the data
    }
    
    # Return internal dict with both formats
    # LangGraph will extract text_for_agent, db_storage will store both
    return {
        "_internal": True,
        "text_for_agent": result_str,
        "json_for_storage": json_data
    }


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    online: Annotated[bool, "to fetch data online or offline"],
    interval: Annotated[str, "Data interval (1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo)"] = None,
) -> str:
    # Get interval from config if not provided
    if interval is None:
        config = get_config()
        interval = config.get("timeframe", "1d")

    curr_date = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_date.strftime("%Y-%m-%d")

    try:
        indicator_value = StockstatsUtils.get_stock_stats(
            symbol,
            indicator,
            curr_date,
            os.path.join(DATA_DIR, "market_data", "price_data"),
            online=online,
            interval=interval,
        )
    except Exception as e:
        print(
            f"Error getting stockstats indicator data for indicator {indicator} on {curr_date}: {e}"
        )
        return ""

    return str(indicator_value)


def get_YFin_data_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    # calculate past days
    date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
    before = date_obj - relativedelta(days=look_back_days)
    start_date = before.strftime("%Y-%m-%d")

    # read in data
    data = pd.read_csv(
        os.path.join(
            DATA_DIR,
            f"market_data/price_data/{symbol}-YFin-data-2015-01-01-2025-03-25.csv",
        )
    )

    # Extract just the date part for comparison
    data["DateOnly"] = data["Date"].str[:10]

    # Filter data between the start and end dates (inclusive)
    filtered_data = data[
        (data["DateOnly"] >= start_date) & (data["DateOnly"] <= curr_date)
    ]

    # Drop the temporary column we created
    filtered_data = filtered_data.drop("DateOnly", axis=1)

    # Set pandas display options to show the full DataFrame
    with pd.option_context(
        "display.max_rows", None, "display.max_columns", None, "display.width", None
    ):
        df_string = filtered_data.to_string()

    return (
        f"## Raw Market Data for {symbol} from {start_date} to {curr_date}:\n\n"
        + df_string
    )


def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"] = None,
    end_date: Annotated[str, "End date in yyyy-mm-dd format"] = None,
    interval: Annotated[str, "Data interval (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1wk, 1mo). If not provided, defaults to timeframe from config (typically 1d)."] = None,
):
    """
    Get stock data online using YFinance data provider with smart caching.
    
    Args:
        symbol: Ticker symbol of the company
        start_date: Start date in yyyy-mm-dd format. If not provided, defaults to market_history_days ago from today.
        end_date: End date in yyyy-mm-dd format. If not provided, defaults to today.
        interval: Data interval. If not provided, defaults to timeframe from config.
        
    Returns:
        Internal dict with text_for_agent and json_for_storage, or error dict
    """
    # Get config
    config = get_config()
    
    # Get interval from config if not provided
    if interval is None:
        interval = config.get("timeframe", "1d")
    
    # Handle start_date and end_date defaults
    if end_date is None:
        end_dt = datetime.now()
        end_date = end_dt.strftime("%Y-%m-%d")
    else:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    
    if start_date is None:
        # Default to market_history_days ago
        lookback_days = config.get('market_history_days', 90)
        start_dt = end_dt - relativedelta(days=lookback_days)
        start_date = start_dt.strftime("%Y-%m-%d")
    else:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")

    # Use YFinanceDataProvider for smart caching
    provider = YFinanceDataProvider()
    
    try:
        # Get data via data provider (uses smart cache)
        data = provider.get_ohlcv_data(
            symbol=symbol,
            start_date=start_dt,
            end_date=end_dt,
            interval=interval
        )
        
        # Check if data is empty
        if data.empty:
            return (
                f"No data found for symbol '{symbol}' between {start_date} and {end_date}"
            )

        # Remove timezone info from Date column if present
        if hasattr(data['Date'], 'dt') and data['Date'].dt.tz is not None:
            data['Date'] = data['Date'].dt.tz_localize(None)

        # Round numerical values to 2 decimal places for cleaner display
        numeric_columns = ["Open", "High", "Low", "Close"]
        for col in numeric_columns:
            if col in data.columns:
                data[col] = data[col].round(2)

        # Set Date as index for CSV output (matches old format)
        data_indexed = data.set_index('Date')
        
        # Convert DataFrame to CSV string
        csv_string = data_indexed.to_csv()

        # Add header information
        header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date} ({interval} interval)\n"
        header += f"# Total records: {len(data)}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        # Store only the PARAMETERS needed to reconstruct this data from cache
        # The UI will use YFinanceDataProvider to get the data using these params
        json_data = {
            "tool": "get_YFin_data_online",
            "symbol": symbol.upper(),
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "total_records": len(data)
        }
        
        # Return internal dict with both formats
        # LangGraph will extract text_for_agent, db_storage will store both
        return {
            "_internal": True,
            "text_for_agent": header + csv_string,
            "json_for_storage": json_data
        }
    except Exception as e:
        logger.error(f"Error fetching data for {symbol}: {e}", exc_info=True)
        # Return error that will stop graph flow
        error_msg = f"CRITICAL ERROR: Failed to fetch market data for {symbol}. {str(e)}"
        return {
            "_internal": True,
            "text_for_agent": error_msg,
            "json_for_storage": {
                "tool": "get_YFin_data_online",
                "error": error_msg,
                "symbol": symbol,
                "interval": interval,
                "start_date": start_date,
                "end_date": end_date
            },
            "_error": True  # Flag to indicate this is an error result
        }


def get_YFin_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    # read in data
    data = pd.read_csv(
        os.path.join(
            DATA_DIR,
            f"market_data/price_data/{symbol}-YFin-data-2015-01-01-2025-03-25.csv",
        )
    )

    if end_date > "2025-03-25":
        raise Exception(
            f"Get_YFin_Data: {end_date} is outside of the data range of 2015-01-01 to 2025-03-25"
        )

    # Extract just the date part for comparison
    data["DateOnly"] = data["Date"].str[:10]

    # Filter data between the start and end dates (inclusive)
    filtered_data = data[
        (data["DateOnly"] >= start_date) & (data["DateOnly"] <= end_date)
    ]

    # Drop the temporary column we created
    filtered_data = filtered_data.drop("DateOnly", axis=1)

    # remove the index from the dataframe
    filtered_data = filtered_data.reset_index(drop=True)

    return filtered_data


# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News (public/insiders, original/processed)",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_sentiment",
            "get_insider_transactions",
        ]
    },
    "macro_data": {
        "description": "Macroeconomic indicators and Federal Reserve data",
        "tools": [
            "get_economic_indicators",
            "get_yield_curve",
            "get_fed_calendar"
        ]
    }
}

VENDOR_LIST = [
    "local",
    "yfinance",
    "openai",
    "google",
    "alpha_vantage",
    "fred"
]

# Mapping of methods to their vendor-specific implementations
# NOTE: For vendors with BA2 providers (see BA2_PROVIDER_MAP), try_ba2_provider() is called first
# These legacy functions serve as fallbacks if BA2 providers fail
VENDOR_METHODS = {
    # core_stock_apis - ALL via BA2 providers
    "get_stock_data": {
        # BA2: ohlcv/alphavantage, ohlcv/yfinance
        "yfinance": get_YFin_data_online,  # Legacy fallback
        "local": get_YFin_data,
    },
    # technical_indicators - ALL via BA2 providers
    "get_indicators": {
        # BA2: indicators/alphavantage, indicators/yfinance
        "yfinance": get_stock_stats_indicators_window,  # Legacy fallback
        "local": get_stock_stats_indicators_window
    },
    # fundamental_data - ALL via BA2 providers
    "get_fundamentals": {
        # BA2: fundamentals_overview/alphavantage, fundamentals_overview/openai
        # No legacy functions - BA2 only
    },
    "get_balance_sheet": {
        # BA2: fundamentals_details/alphavantage
        "yfinance": get_yfinance_balance_sheet,
        "local": get_simfin_balance_sheet,
    },
    "get_cashflow": {
        # BA2: fundamentals_details/alphavantage  
        "yfinance": get_yfinance_cashflow,
        "local": get_simfin_cashflow,
    },
    "get_income_statement": {
        # BA2: fundamentals_details/alphavantage
        "yfinance": get_yfinance_income_statement,
        "local": get_simfin_income_statements,
    },
    # news_data - ALL via BA2 providers
    "get_news": {
        # BA2: news/alphavantage, news/openai, news/google
        "local": get_reddit_company_news,  # Legacy fallback
    },
    "get_global_news": {
        # BA2: news/openai
        "local": get_reddit_global_news
    },
    "get_insider_sentiment": {
        # "local": get_finnhub_company_insider_sentiment  # Disabled - missing finnhub_utils
    },
    "get_insider_transactions": {
        "yfinance": get_yfinance_insider_transactions,
        # "local": get_finnhub_company_insider_transactions,  # Disabled - missing finnhub_utils
    },
    # macro_data
    "get_economic_indicators": {
        # BA2: macro/fred - No legacy implementation
    },
    "get_yield_curve": {
        # BA2: macro/fred - No legacy implementation
    },
    "get_fed_calendar": {
        # BA2: macro/fred - No legacy implementation
    },
}

def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")


# BA2 Provider Integration Functions

def _convert_args_to_ba2(method: str, vendor: str, *args, **kwargs) -> dict:
    """
    Convert TradingAgents method arguments to BA2 provider format.
    
    TradingAgents uses: (ticker, curr_date, look_back_days, ...)
    BA2 uses: (symbol, end_date, lookback_days, format_type, ...)
    """
    ba2_kwargs = {}
    config = get_config()
    
    # Common conversions for methods with symbol parameter
    if method in ["get_news", "get_indicators", "get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"]:
        # Extract symbol/ticker
        if args:
            ba2_kwargs["symbol"] = args[0]  # ticker -> symbol
        elif "ticker" in kwargs:
            ba2_kwargs["symbol"] = kwargs["ticker"]
        elif "symbol" in kwargs:
            ba2_kwargs["symbol"] = kwargs["symbol"]
        
        # Date conversion: curr_date -> end_date
        curr_date_str = None
        if len(args) > 1:
            curr_date_str = args[1]
        elif "curr_date" in kwargs:
            curr_date_str = kwargs["curr_date"]
        elif "end_date" in kwargs:
            curr_date_str = kwargs["end_date"]
        
        if curr_date_str:
            if isinstance(curr_date_str, str):
                ba2_kwargs["end_date"] = datetime.strptime(curr_date_str, "%Y-%m-%d")
            else:
                ba2_kwargs["end_date"] = curr_date_str
        else:
            ba2_kwargs["end_date"] = datetime.now()
        
        # Lookback conversion: look_back_days -> lookback_days
        look_back_days = None
        if len(args) > 2:
            look_back_days = args[2]
        elif "look_back_days" in kwargs:
            look_back_days = kwargs["look_back_days"]
        elif "lookback_days" in kwargs:
            look_back_days = kwargs["lookback_days"]
        
        if look_back_days is not None:
            ba2_kwargs["lookback_days"] = look_back_days
        else:
            # Default from config based on method type
            if method in ["get_news"]:
                ba2_kwargs["lookback_days"] = config.get('news_lookback_days', 7)
            else:
                ba2_kwargs["lookback_days"] = config.get('market_history_days', 90)
    
    # Global news - no symbol needed
    elif method == "get_global_news":
        # Date conversion
        curr_date_str = None
        if args:
            curr_date_str = args[0]
        elif "curr_date" in kwargs:
            curr_date_str = kwargs["curr_date"]
        
        if curr_date_str:
            if isinstance(curr_date_str, str):
                ba2_kwargs["end_date"] = datetime.strptime(curr_date_str, "%Y-%m-%d")
            else:
                ba2_kwargs["end_date"] = curr_date_str
        else:
            ba2_kwargs["end_date"] = datetime.now()
        
        # Lookback days
        look_back_days = None
        if len(args) > 1:
            look_back_days = args[1]
        elif "look_back_days" in kwargs:
            look_back_days = kwargs["look_back_days"]
        elif "lookback_days" in kwargs:
            look_back_days = kwargs["lookback_days"]
        
        ba2_kwargs["lookback_days"] = look_back_days if look_back_days is not None else config.get('news_lookback_days', 7)
    
    # Method-specific conversions
    if method in ["get_news", "get_global_news"]:
        ba2_kwargs["format_type"] = "markdown"  # TradingAgents expects text
    
    elif method == "get_indicators":
        # Extract indicator name
        if len(args) > 1:
            ba2_kwargs["indicator"] = args[1]
        elif "indicator" in kwargs:
            ba2_kwargs["indicator"] = kwargs["indicator"]
        
        # Extract interval
        if "interval" in kwargs:
            ba2_kwargs["interval"] = kwargs["interval"]
        else:
            ba2_kwargs["interval"] = config.get("timeframe", "1d")
        
        ba2_kwargs["format_type"] = "markdown"  # TradingAgents expects text
    
    elif method == "get_fundamentals":
        # Fundamentals overview - just needs symbol and format
        ba2_kwargs["format_type"] = "markdown"
    
    elif method in ["get_balance_sheet", "get_cashflow", "get_income_statement"]:
        # Fundamentals details methods: (ticker, freq, curr_date) -> (symbol, frequency, format_type)
        # Frequency
        if len(args) > 1:
            ba2_kwargs["frequency"] = args[1]
        elif "freq" in kwargs:
            ba2_kwargs["frequency"] = kwargs["freq"]
        else:
            ba2_kwargs["frequency"] = "quarterly"
        
        ba2_kwargs["format_type"] = "markdown"
    
    elif method in ["get_economic_indicators", "get_yield_curve", "get_fed_calendar"]:
        # Macro methods: (curr_date, lookback_days) -> (end_date, lookback_days, format_type)
        # Date conversion
        curr_date_str = None
        if args:
            curr_date_str = args[0]
        elif "curr_date" in kwargs:
            curr_date_str = kwargs["curr_date"]
        
        if curr_date_str:
            if isinstance(curr_date_str, str):
                ba2_kwargs["end_date"] = datetime.strptime(curr_date_str, "%Y-%m-%d")
            else:
                ba2_kwargs["end_date"] = curr_date_str
        else:
            ba2_kwargs["end_date"] = datetime.now()
        
        # Lookback days
        look_back_days = None
        if len(args) > 1:
            look_back_days = args[1]
        elif "look_back_days" in kwargs:
            look_back_days = kwargs["look_back_days"]
        elif "lookback_days" in kwargs:
            look_back_days = kwargs["lookback_days"]
        
        ba2_kwargs["lookback_days"] = look_back_days if look_back_days is not None else config.get('economic_data_days', 90)
        ba2_kwargs["format_type"] = "markdown"
    
    return ba2_kwargs


def try_ba2_provider(method: str, vendor: str, *args, **kwargs):
    """
    Use BA2 provider system (no fallbacks - raises errors if provider fails).
    
    Returns:
        result: Provider result (dict or markdown string)
        
    Raises:
        ValueError: If provider is not configured or fails
    """
    # Check if this method+vendor combo has a BA2 provider
    provider_key = (method, vendor)
    if provider_key not in BA2_PROVIDER_MAP:
        raise ValueError(f"No BA2 provider configured for {method}/{vendor}")
    
    category, provider_name = BA2_PROVIDER_MAP[provider_key]
    
    # Convert arguments to BA2 format
    ba2_kwargs = _convert_args_to_ba2(method, vendor, *args, **kwargs)
    
    # Get the provider
    provider = get_provider(category, provider_name)
    
    # Wrap with persistence
    wrapper = ProviderWithPersistence(provider, category)
    
    # Determine cache key and method to call
    symbol = ba2_kwargs.get("symbol", "unknown")
    lookback = ba2_kwargs.get("lookback_days", 7)
    
    if method == "get_stock_data":
        cache_key = f"{symbol}_ohlcv_{lookback}days"
        ba2_method = "get_ohlcv_data"  # MarketDataProvider method (renamed from get_dataframe)
        max_age_hours = 24
    elif method == "get_news":
        cache_key = f"{symbol}_news_{lookback}days"
        ba2_method = "get_company_news"
        max_age_hours = 2  # News cache for 2 hours
    elif method == "get_global_news":
        cache_key = f"global_news_{lookback}days"
        ba2_method = "get_global_news"
        max_age_hours = 2  # News cache for 2 hours
    elif method == "get_indicators":
        indicator = ba2_kwargs.get("indicator", "unknown")
        interval = ba2_kwargs.get("interval", "1d")
        cache_key = f"{symbol}_{indicator}_{interval}_{lookback}days"
        ba2_method = "get_indicator"
        max_age_hours = 6  # Indicators cache for 6 hours
    elif method == "get_fundamentals":
        cache_key = f"{symbol}_fundamentals_overview"
        ba2_method = "get_fundamentals_overview"
        max_age_hours = 24  # Fundamentals cache for 24 hours
    elif method == "get_balance_sheet":
        frequency = ba2_kwargs.get("frequency", "quarterly")
        cache_key = f"{symbol}_balance_sheet_{frequency}"
        ba2_method = "get_balance_sheet"
        max_age_hours = 24
    elif method == "get_cashflow":
        frequency = ba2_kwargs.get("frequency", "quarterly")
        cache_key = f"{symbol}_cashflow_{frequency}"
        ba2_method = "get_cashflow_statement"
        max_age_hours = 24
    elif method == "get_income_statement":
        frequency = ba2_kwargs.get("frequency", "quarterly")
        cache_key = f"{symbol}_income_statement_{frequency}"
        ba2_method = "get_income_statement"
        max_age_hours = 24
    elif method == "get_economic_indicators":
        cache_key = f"economic_indicators_{lookback}days"
        ba2_method = "get_economic_indicators"
        max_age_hours = 12  # Macro data cache for 12 hours
    elif method == "get_yield_curve":
        cache_key = f"yield_curve_{lookback}days"
        ba2_method = "get_yield_curve"
        max_age_hours = 12
    elif method == "get_fed_calendar":
        cache_key = f"fed_calendar_{lookback}days"
        ba2_method = "get_fed_calendar"
        max_age_hours = 12
    else:
        raise ValueError(f"Unknown method: {method}")
    
    # Call BA2 provider with caching
    ba2_logger.debug(f"Calling BA2 provider: {category}/{provider_name}.{ba2_method}({symbol}, lookback={lookback})")
    result = wrapper.fetch_with_cache(
        ba2_method,
        cache_key,
        max_age_hours=max_age_hours,
        **ba2_kwargs
    )
    
    ba2_logger.debug(f"BA2 provider succeeded: {category}/{provider_name}")
    return result


def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation.
    
    For news methods (get_news, get_global_news): Collects data from ALL configured vendors except 'local'.
    For other methods: Uses fallback logic (tries vendors in order until one succeeds).
    """
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)

    # Handle comma-separated vendors
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    # News methods: Collect from ALL vendors (except local)
    is_news_method = method in ["get_news", "get_global_news"]
    
    if is_news_method:
        # For news, use only primary vendors (exclude local)
        vendors_to_use = [v for v in primary_vendors if v != "local"]
        print(f"DEBUG: {method} - Collecting from ALL configured vendors: [{' + '.join(vendors_to_use)}]")
    else:
        # For non-news methods: Create fallback vendor list
        all_available_vendors = list(VENDOR_METHODS[method].keys())
        vendors_to_use = primary_vendors.copy()
        for vendor in all_available_vendors:
            if vendor not in vendors_to_use:
                vendors_to_use.append(vendor)
        
        primary_str = " → ".join(primary_vendors)
        fallback_str = " → ".join(vendors_to_use)
        print(f"DEBUG: {method} - Primary: [{primary_str}] | Full fallback order: [{fallback_str}]")

    # Track results and execution state
    results = []
    vendor_attempt_count = 0
    any_primary_vendor_attempted = False
    successful_vendor = None

    for vendor in vendors_to_use:
        if vendor not in VENDOR_METHODS[method]:
            if vendor in primary_vendors:
                print(f"INFO: Vendor '{vendor}' not supported for method '{method}', skipping")
            continue

        vendor_impl = VENDOR_METHODS[method][vendor]
        is_primary_vendor = vendor in primary_vendors
        vendor_attempt_count += 1

        # Track if we attempted any primary vendor
        if is_primary_vendor:
            any_primary_vendor_attempted = True

        # Use BA2 provider if available (no fallback - errors will propagate)
        if (method, vendor) in BA2_PROVIDER_MAP:
            print(f"DEBUG: Using BA2 provider for {method} with vendor '{vendor}'")
            try:
                result = try_ba2_provider(method, vendor, *args, **kwargs)
                results.append(result)
                successful_vendor = vendor
                print(f"SUCCESS: BA2 provider for {method}/{vendor} completed (with database persistence)")
                
                # For single-vendor configs, stop after success
                if not is_news_method and len(primary_vendors) == 1:
                    print(f"DEBUG: Stopping after successful vendor '{vendor}' (single-vendor config)")
                    break
                # For news or multi-vendor, continue to collect from all vendors
                continue
            except AlphaVantageRateLimitError as e:
                print(f"RATE_LIMIT: Alpha Vantage rate limit exceeded: {e}")
                # Continue to next vendor
                continue
            except Exception as e:
                print(f"FAILED: BA2 provider {method}/{vendor} failed: {e}")
                # Continue to next vendor
                continue
        
        # Legacy fallback for vendors without BA2 providers
        if vendor not in VENDOR_METHODS[method]:
            print(f"INFO: No provider available for {method}/{vendor}")
            continue

        # Debug: Print current attempt
        if is_news_method:
            print(f"DEBUG: Collecting from vendor '{vendor}' for {method} (source #{vendor_attempt_count})")
        else:
            vendor_type = "PRIMARY" if is_primary_vendor else "FALLBACK"
            print(f"DEBUG: Attempting {vendor_type} vendor '{vendor}' for {method} (attempt #{vendor_attempt_count})")

        vendor_impl = VENDOR_METHODS[method][vendor]
        
        # Handle list of methods for a vendor
        if isinstance(vendor_impl, list):
            vendor_methods = [(impl, vendor) for impl in vendor_impl]
            print(f"DEBUG: Vendor '{vendor}' has multiple implementations: {len(vendor_methods)} functions")
        else:
            vendor_methods = [(vendor_impl, vendor)]

        # Run methods for this vendor
        vendor_results = []
        for impl_func, vendor_name in vendor_methods:
            try:
                print(f"DEBUG: Calling {impl_func.__name__} from vendor '{vendor_name}'...")
                result = impl_func(*args, **kwargs)
                vendor_results.append(result)
                print(f"SUCCESS: {impl_func.__name__} from vendor '{vendor_name}' completed successfully")
            except Exception as e:
                # Log error but continue with other implementations
                print(f"FAILED: {impl_func.__name__} from vendor '{vendor_name}' failed: {e}")
                continue

        # Add this vendor's results
        if vendor_results:
            results.extend(vendor_results)
            successful_vendor = vendor
            result_summary = f"Got {len(vendor_results)} result(s)"
            print(f"SUCCESS: Vendor '{vendor}' succeeded - {result_summary}")
            
            # Stopping logic: For news methods, ALWAYS continue to collect from all vendors
            # For non-news methods, stop after first successful vendor for single-vendor configs
            if not is_news_method and len(primary_vendors) == 1:
                print(f"DEBUG: Stopping after successful vendor '{vendor}' (single-vendor config)")
                break
        else:
            print(f"FAILED: Vendor '{vendor}' produced no results")

    # Final result summary
    if not results:
        print(f"FAILURE: All {vendor_attempt_count} vendor attempts failed for method '{method}'")
        raise RuntimeError(f"All vendor implementations failed for method '{method}'")
    else:
        if is_news_method:
            print(f"FINAL: Method '{method}' collected {len(results)} result(s) from {vendor_attempt_count} source(s)")
        else:
            print(f"FINAL: Method '{method}' completed with {len(results)} result(s) from {vendor_attempt_count} vendor attempt(s)")

    # Return single result if only one, otherwise concatenate as string
    if len(results) == 1:
        return results[0]
    else:
        # Convert all results to strings and concatenate
        return '\n\n---\n\n'.join(str(result) for result in results)
