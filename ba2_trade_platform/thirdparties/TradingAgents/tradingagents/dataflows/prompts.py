"""
Prompts for OpenAI-based data gathering tools.

This module contains all the prompts used by the TradingAgents dataflows
to query OpenAI for market data, news, and fundamental analysis.
"""

from datetime import datetime
from dateutil.relativedelta import relativedelta


def get_stock_news_prompt(ticker: str, curr_date: str, lookback_days: int = 7) -> str:
    """
    Generate prompt for searching social media and news for a specific stock.
    
    Args:
        ticker: Stock ticker symbol
        curr_date: Current date in YYYY-MM-DD format
        lookback_days: Number of days to look back (default: 7, should use social_sentiment_days from config)
        
    Returns:
        Formatted prompt string
    """
    date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (date_obj - relativedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    return (
        f"Search social media for {ticker} from {start_date} to {curr_date}. "
        f"Return posts, tweets, and social media mentions about {ticker} with timestamps and sentiment. "
        f"Focus on official accounts, financial news sources, and relevant discussions."
    )


def get_global_news_prompt(curr_date: str, lookback_days: int = 7) -> str:
    """
    Generate prompt for searching global macroeconomic news.
    
    Args:
        curr_date: Current date in YYYY-MM-DD format
        lookback_days: Number of days to look back (default: 7, should use news_lookback_days from config)
        
    Returns:
        Formatted prompt string
    """
    date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (date_obj - relativedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    return (
        f"Search for global macroeconomic news and market-moving events from {start_date} to {curr_date} "
        f"that would be informative for trading purposes. "
        f"Include: Federal Reserve decisions, inflation data, GDP reports, employment numbers, "
        f"central bank policies, geopolitical events, and major economic indicators. "
        f"Make sure you only retrieve data posted during the specified period ({start_date} to {curr_date}). "
        f"Summarize the key findings in a clear, concise format."
    )


def get_fundamentals_prompt(ticker: str, curr_date: str, lookback_days: int) -> str:
    """
    Generate prompt for searching fundamental analysis and financial metrics.
    
    Args:
        ticker: Stock ticker symbol
        curr_date: Current date in YYYY-MM-DD format
        lookback_days: Number of days to look back for fundamental data
        
    Returns:
        Formatted prompt string
    """
    date_obj = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (date_obj - relativedelta(days=lookback_days)).strftime("%Y-%m-%d")
    
    return (
        f"Search all available financial sources (news sites, financial forums, analyst reports, SEC filings, "
        f"financial data providers) for fundamental analysis and discussions about {ticker} "
        f"from {start_date} to {curr_date}. "
        f"\n\n"
        f"**Search Requirements:**\n"
        f"1. **Date Range**: Only include information published between {start_date} and {curr_date}\n"
        f"2. **Sources**: Search across all available sources including:\n"
        f"   - Financial news websites (Bloomberg, Reuters, WSJ, CNBC, etc.)\n"
        f"   - Investment research platforms (Seeking Alpha, Motley Fool, etc.)\n"
        f"   - Analyst reports and ratings\n"
        f"   - SEC filings and earnings reports\n"
        f"   - Financial forums and discussion boards\n"
        f"   - Company investor relations materials\n"
        f"\n"
        f"**Metrics to Include (all available):**\n"
        f"- Valuation: P/E (trailing & forward), P/S, P/B, PEG ratio, EV/EBITDA\n"
        f"- Profitability: Net margin, operating margin, ROE, ROA, ROIC\n"
        f"- Growth: Revenue growth (YoY, QoQ), earnings growth, EPS growth\n"
        f"- Cash Flow: Operating cash flow (TTM), free cash flow, FCF yield\n"
        f"- Financial Health: Current ratio, debt-to-equity, interest coverage\n"
        f"- Market Data: Market cap, shares outstanding, float\n"
        f"- Dividend: Dividend yield, payout ratio, dividend growth rate\n"
        f"- Other: Analyst ratings, price targets, earnings estimates\n"
        f"\n"
        f"**Output Format:**\n"
        f"Provide a summary of the most relevant fundamental information found, organized as a markdown table. "
        f"Include:\n"
        f"- Key financial metrics with values and dates\n"
        f"- Notable changes or trends identified\n"
        f"- Analyst consensus and price targets\n"
        f"- Recent earnings results or guidance\n"
        f"- Any significant fundamental events or announcements\n"
        f"\n"
        f"Focus on actionable insights and material information. Exclude outdated or irrelevant data."
    )
