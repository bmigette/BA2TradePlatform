from typing import Annotated
from datetime import datetime
from dateutil.relativedelta import relativedelta
from .googlenews_utils import getNewsData
from .config import get_config


def get_google_news(
    query: Annotated[str, "Query to search with"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[
        int,
        "Number of days to look back for news. If not provided, defaults to news_lookback_days from config (typically 7 days). You can specify a custom value to get more or less historical news."
    ] = None,
) -> str:
    """Search for news using Google News.
    
    Args:
        query: Search query
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. Defaults to news_lookback_days from config if None.
        
    Returns:
        Formatted news results
    """
    # Get lookback days from config if not provided
    if look_back_days is None:
        config = get_config()
        look_back_days = config.get('news_lookback_days', 7)
        
    query = query.replace(" ", "+")

    start_date = datetime.strptime(curr_date, "%Y-%m-%d")
    before = start_date - relativedelta(days=look_back_days)
    before = before.strftime("%Y-%m-%d")

    news_results = getNewsData(query, before, curr_date)

    news_str = ""

    for news in news_results:
        news_str += (
            f"### {news['title']} (source: {news['source']}) \n\n{news['snippet']}\n\n"
        )

    if len(news_results) == 0:
        return ""

    return f"## {query} Google News, from {before} to {curr_date}:\n\n{news_str}"
