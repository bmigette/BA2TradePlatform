"""
Google News Provider

Provides news scraping from Google News for company-specific queries.
"""

from typing import Dict, Any, Literal, Optional
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
import time
import random
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_result,
)

from ba2_trade_platform.core.interfaces import MarketNewsInterface
from ba2_trade_platform.core.provider_utils import (
    validate_date_range,
    validate_lookback_days,
    log_provider_call,
    calculate_date_range
)
from ba2_trade_platform.logger import logger


class GoogleNewsProvider(MarketNewsInterface):
    """
    Google News Scraping Provider.
    
    Provides access to news articles by scraping Google News search results.
    Good for getting diverse news sources and recent articles.
    """
    
    def __init__(self):
        """Initialize the Google News Provider."""
        super().__init__()
        logger.info("GoogleNewsProvider initialized successfully")
    
    @staticmethod
    def _is_rate_limited(response):
        """Check if the response indicates rate limiting (status code 429)."""
        return response.status_code == 429
    
    @retry(
        retry=(retry_if_result(lambda r: r.status_code == 429)),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        stop=stop_after_attempt(5),
    )
    def _make_request(self, url: str, headers: Dict[str, str]):
        """Make a request with retry logic for rate limiting."""
        # Random delay before each request to avoid detection
        time.sleep(random.uniform(2, 6))
        response = requests.get(url, headers=headers)
        return response
    
    def _scrape_google_news(self, query: str, start_date: str, end_date: str) -> list:
        """
        Scrape Google News search results for a given query and date range.
        
        Args:
            query: Search query
            start_date: Start date in yyyy-mm-dd format
            end_date: End date in yyyy-mm-dd format
            
        Returns:
            List of news articles with title, link, snippet, date, source
        """
        # Convert date format from yyyy-mm-dd to mm/dd/yyyy
        if "-" in start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            start_date = start_dt.strftime("%m/%d/%Y")
        if "-" in end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            end_date = end_dt.strftime("%m/%d/%Y")
        
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
            # Add cookie consent to bypass the consent page
            "Cookie": "CONSENT=YES+cb.20210720-07-p0.en+FX+410; SOCS=CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo-KnBg",
        }
        
        news_results = []
        page = 0
        
        while True:
            offset = page * 10
            url = (
                f"https://www.google.com/search?q={query}"
                f"&tbs=cdr:1,cd_min:{start_date},cd_max:{end_date}"
                f"&tbm=nws&start={offset}"
            )
            
            try:
                response = self._make_request(url, headers)
                soup = BeautifulSoup(response.content, "html.parser")
                
                # Check if we got redirected to consent page
                base_tag = soup.find('base')
                if base_tag and 'consent.google.com' in str(base_tag.get('href', '')):
                    logger.warning("Google is showing consent page. Google News scraping may be blocked.")
                    logger.warning("Consider using alternative news sources (OpenAI, Alpha Vantage, etc.)")
                    break
                
                # Also check the URL (in case of redirect)
                if 'consent.google.com' in response.url:
                    logger.warning("Redirected to Google consent page. Google News scraping may be blocked.")
                    logger.warning("Consider using alternative news sources (OpenAI, Alpha Vantage, etc.)")
                    break
                
                # Try multiple selectors (Google changes these frequently)
                selectors_to_try = [
                    "div.SoaBEf",  # Original selector
                    "div.Gx5Zad",  # Alternative 1
                    "div.dbsr",    # Alternative 2
                    "div.n0jPhd",  # Alternative 3
                ]
                
                results_on_page = []
                for selector in selectors_to_try:
                    results_on_page = soup.select(selector)
                    if results_on_page:
                        if page == 0:  # Only log on first page
                            logger.debug(f"Using selector: {selector} ({len(results_on_page)} results)")
                        break
                
                if not results_on_page:
                    logger.warning("No news elements found. Google News HTML structure may have changed.")
                    break
                
                for el in results_on_page:
                    try:
                        link = el.find("a")["href"]
                        
                        # Try multiple selectors for title
                        title_elem = el.select_one("div.MBeuO") or el.select_one("div.n0jPhd") or el.select_one("div.mCBkyc")
                        if not title_elem:
                            continue
                        title = title_elem.get_text()
                        
                        # Try multiple selectors for snippet
                        snippet_elem = el.select_one(".GI74Re") or el.select_one(".Y3v8qd") or el.select_one(".s3v9rd")
                        snippet = snippet_elem.get_text() if snippet_elem else ""
                        
                        # Try multiple selectors for date
                        date_elem = el.select_one(".LfVVr") or el.select_one(".OSrXXb") or el.select_one("span.r0bn4c")
                        date = date_elem.get_text() if date_elem else ""
                        
                        # Try multiple selectors for source
                        source_elem = el.select_one(".NUnG9d span") or el.select_one(".CEMjEf") or el.select_one("span.vr1PYe")
                        source = source_elem.get_text() if source_elem else ""
                        
                        news_results.append({
                            "link": link,
                            "title": title,
                            "snippet": snippet,
                            "date": date,
                            "source": source,
                        })
                    except Exception as e:
                        logger.debug(f"Error processing result: {e}")
                        continue
                
                # Check for the "Next" link (pagination)
                next_link = soup.find("a", id="pnnext")
                if not next_link:
                    break
                
                page += 1
                
            except Exception as e:
                logger.error(f"Failed after multiple retries: {e}")
                break
        
        return news_results
    
    def get_provider_name(self) -> str:
        """Get the provider name."""
        return "google"
    
    def get_supported_features(self) -> Dict[str, Any]:
        """Get supported features of this provider."""
        return {
            "company_news": True,
            "global_news": True,
            "sentiment_analysis": False,  # No sentiment from Google News scraping
            "full_article_content": False,  # Only snippets available
            "image_urls": False,
            "source_attribution": True,
            "max_lookback_days": 30,  # Google News typically shows recent results
            "rate_limits": {
                "requests_per_minute": 60,
                "notes": "Web scraping - use responsibly to avoid rate limiting"
            }
        }
    
    @log_provider_call
    def get_company_news(
        self,
        symbol: str,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get news articles for a specific company by scraping Google News.
        
        Args:
            symbol: Stock ticker symbol (used as search query)
            end_date: End date (inclusive)
            start_date: Start date (use either this OR lookback_days, not both)
            lookback_days: Days to look back from end_date
            limit: Maximum number of articles (not enforced by Google)
            format_type: Output format ('dict' or 'markdown')
        
        Returns:
            News data in requested format
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=30)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=30)
        
        logger.info(
            f"Fetching Google News for {symbol} from {start_date.date()} to {end_date.date()}"
        )
        
        try:
            # Use symbol as search query
            query = symbol.replace(" ", "+")
            
            # Call Google News scraper
            start_date_str = start_date.strftime("%Y-%m-%d")
            end_date_str = end_date.strftime("%Y-%m-%d")
            
            news_results = self._scrape_google_news(query, start_date_str, end_date_str)
            
            # Convert to standard format
            articles = []
            for news in news_results:
                article = {
                    "title": news.get('title', ''),
                    "summary": news.get('snippet', ''),
                    "source": news.get('source', ''),
                    "author": None,  # Not available from Google News scraping
                    "published_at": news.get('date', ''),
                    "url": news.get('link', ''),
                    "image_url": None,  # Not available
                    "symbols": [symbol]  # Assume symbol is mentioned
                }
                articles.append(article)
            
            result = {
                "symbol": symbol,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(articles),
                "articles": articles
            }
            
            logger.info(f"Retrieved {len(articles)} Google News articles for {symbol}")
            
            # Format output
            if format_type == "dict":
                return result
            else:
                return self._format_as_markdown(result, is_company_news=True)
            
        except Exception as e:
            logger.error(f"Error fetching Google News for {symbol}: {e}", exc_info=True)
            raise
    
    @log_provider_call
    def get_global_news(
        self,
        end_date: datetime,
        start_date: Optional[datetime] = None,
        lookback_days: Optional[int] = None,
        limit: int = 50,
        format_type: Literal["dict", "markdown"] = "markdown"
    ) -> Dict[str, Any] | str:
        """
        Get global/market news from Google News.
        
        Uses general market-related search queries.
        """
        # Validate date parameters
        if start_date and lookback_days:
            raise ValueError("Provide either start_date OR lookback_days, not both")
        if not start_date and not lookback_days:
            raise ValueError("Must provide either start_date or lookback_days")
        
        # Calculate date range
        if lookback_days:
            lookback_days = validate_lookback_days(lookback_days, max_lookback=30)
            start_date, end_date = calculate_date_range(end_date, lookback_days)
        else:
            start_date, end_date = validate_date_range(start_date, end_date, max_days=30)
        
        logger.info(
            f"Fetching global Google News from {start_date.date()} to {end_date.date()}"
        )
        
        try:
            # Use generic market query
            query = "stock+market+news"
            
            # Call Google News scraper
            start_date_str = start_date.strftime("%Y-%m-%d")
            end_date_str = end_date.strftime("%Y-%m-%d")
            
            news_results = self._scrape_google_news(query, start_date_str, end_date_str)
            
            # Convert to standard format
            articles = []
            for news in news_results:
                article = {
                    "title": news.get('title', ''),
                    "summary": news.get('snippet', ''),
                    "source": news.get('source', ''),
                    "author": None,
                    "published_at": news.get('date', ''),
                    "url": news.get('link', ''),
                    "image_url": None,
                    "symbols": []  # No specific symbols for global news
                }
                articles.append(article)
            
            result = {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "article_count": len(articles),
                "articles": articles
            }
            
            logger.info(f"Retrieved {len(articles)} global Google News articles")
            
            # Format output
            if format_type == "dict":
                return result
            else:
                return self._format_as_markdown(result, is_company_news=False)
            
        except Exception as e:
            logger.error(f"Error fetching global Google News: {e}", exc_info=True)
            raise
    
    def _format_as_markdown(self, data: Dict[str, Any], is_company_news: bool) -> str:
        """Format news data as markdown."""
        lines = []
        
        # Header
        if is_company_news:
            lines.append(f"# Google News for {data['symbol']}")
        else:
            lines.append("# Global Market News from Google")
        
        lines.append(f"**Period:** {data['start_date'][:10]} to {data['end_date'][:10]}")
        lines.append(f"**Articles:** {data['article_count']}")
        lines.append("")
        
        # Articles
        for i, article in enumerate(data["articles"], 1):
            lines.append(f"## {i}. {article['title']}")
            lines.append(f"**Source:** {article['source']}")
            
            if article.get('published_at'):
                lines.append(f"**Date:** {article['published_at']}")
            
            lines.append("")
            lines.append(article['summary'])
            
            if article.get('url'):
                lines.append(f"\n[Read more]({article['url']})")
            
            lines.append("")
            lines.append("---")
            lines.append("")
        
        return "\n".join(lines)
