"""Pre-fetch context gatherers for the single-shot (non-agentic) analysts.

Instead of giving the LLM tools and letting it decide what to call across multiple
turns, we gather each analyst's bounded data set up-front (deterministic, one LLM
round-trip) and inject it into the prompt. The Market/Technical analyst stays
agentic (it benefits from on-demand indicator calls); fundamentals/news/social/
macro are pre-fetched here.

Each gatherer is defensive: a failing section is logged and skipped so one bad
provider call never blanks the whole context.
"""
from ba2_trade_platform.logger import logger


def _section(parts, title, fn):
    """Run fn() and append '# title\n\n<result>' to parts if it returns content."""
    try:
        result = fn()
        if result and isinstance(result, str) and result.strip():
            parts.append(f"# {title}\n\n{result.strip()}")
    except Exception as e:
        logger.warning(f"prefetch: section '{title}' failed: {e}")


def gather_fundamentals_context(toolkit, ticker, current_date):
    """Profile + ratios/key-metrics + statements + earnings + insider for one ticker."""
    parts = []
    _section(parts, "Company Profile", lambda: toolkit.get_company_profile(ticker, current_date))
    _section(parts, "Financial Ratios & Key Metrics (TTM)", lambda: toolkit.get_financial_ratios(ticker, current_date))
    _section(parts, "Income Statement (quarterly, last 4)", lambda: toolkit.get_income_statement(ticker, "quarterly", current_date, 4))
    _section(parts, "Balance Sheet (quarterly, last 4)", lambda: toolkit.get_balance_sheet(ticker, "quarterly", current_date, 4))
    _section(parts, "Cash Flow (quarterly, last 4)", lambda: toolkit.get_cashflow_statement(ticker, "quarterly", current_date, 4))
    _section(parts, "Past Earnings (last 8 quarters)", lambda: toolkit.get_past_earnings(ticker, current_date, 8, "quarterly"))
    _section(parts, "Forward Earnings Estimates", lambda: toolkit.get_earnings_estimates(ticker, current_date, 4, "quarterly"))
    _section(parts, "Insider Sentiment", lambda: toolkit.get_insider_sentiment(ticker, current_date, None))
    _section(parts, "Insider Transactions", lambda: toolkit.get_insider_transactions(ticker, current_date, None))
    return "\n\n---\n\n".join(parts) if parts else "No fundamental data available."


def gather_news_context(toolkit, ticker, current_date):
    """Company-specific news + global/macro news."""
    parts = []
    _section(parts, f"Company News — {ticker}", lambda: toolkit.get_company_news(ticker, current_date, None))
    _section(parts, "Global / Macro News", lambda: toolkit.get_global_news(current_date, None))
    return "\n\n---\n\n".join(parts) if parts else "No news data available."


def gather_social_context(toolkit, ticker, current_date):
    """Social-media sentiment + recent company news."""
    parts = []
    _section(parts, f"Social Media Sentiment — {ticker}", lambda: toolkit.get_social_media_sentiment(ticker, current_date, None))
    _section(parts, f"Recent Company News — {ticker}", lambda: toolkit.get_company_news(ticker, current_date, None))
    return "\n\n---\n\n".join(parts) if parts else "No social/sentiment data available."


def gather_macro_context(toolkit, current_date):
    """Economic indicators + yield curve + Fed calendar."""
    parts = []
    _section(parts, "Economic Indicators", lambda: toolkit.get_economic_indicators(current_date, None, None))
    _section(parts, "Treasury Yield Curve", lambda: toolkit.get_yield_curve(current_date, None))
    _section(parts, "Federal Reserve Calendar", lambda: toolkit.get_fed_calendar(current_date, None))
    return "\n\n---\n\n".join(parts) if parts else "No macroeconomic data available."
