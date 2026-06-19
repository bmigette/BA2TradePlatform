"""
Tools API endpoints

Provides endpoints for testing and debugging various providers.
"""

from fastapi import APIRouter, HTTPException, status, Query
from fastapi.responses import JSONResponse
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path
import logging
import json
import uuid
import pandas as pd

from app.services.sentiment import SentimentService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/news/fetch")
async def fetch_news(
    symbol: Optional[str] = Query(None, description="Stock ticker symbol (e.g., AAPL). Leave empty for global news."),
    provider: str = Query("fmp", description="News provider (fmp, alpaca, alphavantage)"),
    news_type: str = Query("company", description="Type of news: 'company' (requires symbol) or 'global' (market/general news)"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD). If not provided, defaults to 30 days ago."),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD). If not provided, defaults to today."),
    days: Optional[int] = Query(None, description="Deprecated: Use start_date/end_date instead. Number of days to look back."),
    limit: int = Query(500, description="Maximum number of articles")
):
    """
    Fetch news articles for a symbol or global market news.

    Args:
        symbol: Stock ticker symbol (required for company news, optional for global)
        provider: News provider to use
        news_type: 'company' for ticker-specific news, 'global' for market news
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        days: Deprecated, use date range instead
        limit: Maximum articles to return

    Returns:
        List of news articles
    """
    try:
        # Validate inputs
        if news_type == "company" and not symbol:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Symbol is required for company news"
            )

        # Parse dates or use defaults
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            end_dt = datetime.now()

        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        elif days:
            # Legacy support for days parameter
            start_dt = end_dt - timedelta(days=days)
        else:
            # Default to last 30 days
            start_dt = end_dt - timedelta(days=30)

        logger.info(f"Fetching {news_type} news for {symbol or 'global'} from {provider}, {start_dt.date()} to {end_dt.date()}, limit={limit}")

        sentiment_service = SentimentService()

        if news_type == "global":
            # Fetch global/market news
            articles = sentiment_service.fetch_global_news(
                start_date=start_dt,
                end_date=end_dt,
                provider=provider,
                limit=limit
            )
        else:
            # Fetch company-specific news
            articles = sentiment_service.fetch_news_for_ticker(
                ticker=symbol,
                start_date=start_dt,
                end_date=end_dt,
                provider=provider,
                enrich_content=False,
                limit=limit
            )

        # Convert dates to strings for JSON serialization
        for article in articles:
            if isinstance(article.get('date'), datetime):
                article['date'] = article['date'].isoformat()
            if isinstance(article.get('published_at'), datetime):
                article['published_at'] = article['published_at'].isoformat()

        return {
            "symbol": symbol or "global",
            "news_type": news_type,
            "provider": provider,
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "article_count": len(articles),
            "articles": articles
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching news: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch news: {str(e)}"
        )


@router.post("/news/analyze-single")
async def analyze_single_article(
    title: str = Query(..., description="Article title"),
    content: str = Query("", description="Article content/summary")
):
    """
    Analyze sentiment for a single news article.

    Args:
        title: Article title
        content: Article content or summary

    Returns:
        Sentiment analysis result
    """
    try:
        logger.info(f"Analyzing sentiment for article: {title[:50]}...")

        sentiment_service = SentimentService()

        # Create a mock article for analysis
        article = {
            'title': title,
            'summary': content,
            'content': content,
            'date': datetime.now()
        }

        # Analyze the article
        analyzed = sentiment_service.analyze_news_articles([article])

        if analyzed:
            result = analyzed[0]
            return {
                "title": title,
                "sentiment": result.get('sentiment', 'neutral'),
                "sentiment_score": result.get('sentiment_score', 0.5),
                "confidence": result.get('confidence', 0.0),
                "model_used": "FinBERT"
            }
        else:
            return {
                "title": title,
                "sentiment": "neutral",
                "sentiment_score": 0.5,
                "confidence": 0.0,
                "error": "Analysis returned no results"
            }

    except Exception as e:
        logger.error(f"Error analyzing sentiment: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to analyze sentiment: {str(e)}"
        )


@router.get("/news/providers")
async def list_news_providers():
    """
    List available news providers and their status.

    Returns:
        List of providers with availability info
    """
    import os

    providers = [
        {
            "id": "fmp",
            "name": "Financial Modeling Prep",
            "description": "Company and market news from FMP API",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("FMP_API_KEY")),
            "features": ["company_news", "market_news"],
            "has_sentiment": False
        },
        {
            "id": "alphavantage",
            "name": "Alpha Vantage",
            "description": "News with built-in sentiment analysis from Alpha Vantage API",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("ALPHA_VANTAGE_API_KEY")),
            "features": ["company_news", "sentiment_analysis"],
            "has_sentiment": True
        },
        {
            "id": "finnhub",
            "name": "Finnhub",
            "description": "Company and market news from Finnhub API",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("FINNHUB_API_KEY")),
            "features": ["company_news", "global_news"],
            "has_sentiment": False
        },
        {
            "id": "alpaca",
            "name": "Alpaca Markets",
            "description": "News from Alpaca trading platform",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("ALPACA_API_KEY")),
            "features": ["company_news"],
            "has_sentiment": False
        },
        {
            "id": "localfiles",
            "name": "Local Files",
            "description": "Read from previously exported JSON files",
            "requires_api_key": False,
            "api_key_configured": True,
            "features": ["company_news", "cached_sentiment"],
            "has_sentiment": True
        }
    ]

    return {
        "providers": providers,
        "default": "fmp"
    }


# Directory for exported news files + trained models (test bucket, app.paths;
# not the repo/CWD — nothing is cached inside the repo anymore).
from app.paths import NEWS_EXPORTS_DIR, MODELS_DIR


@router.post("/news/export")
async def export_news_to_json(
    symbol: Optional[str] = Query(None, description="Stock ticker symbol (required for company news)"),
    provider: str = Query(..., description="Provider used to fetch the news"),
    news_type: str = Query("company", description="Type of news: 'company' or 'global'"),
    articles: List[Dict[str, Any]] = None
):
    """
    Export news articles to a JSON file with standardized format.

    The exported format can be imported using the LocalFiles news provider.

    Args:
        symbol: Stock ticker symbol (required for company news, ignored for global)
        provider: Original provider name
        news_type: Type of news ('company' or 'global')
        articles: List of articles to export

    Returns:
        Export file path and metadata
    """
    if not articles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No articles provided for export"
        )

    if news_type == "company" and not symbol:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Symbol is required for company news export"
        )

    try:
        # Ensure export directory exists
        NEWS_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

        # Generate filename based on news type
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if news_type == "global":
            filename = f"global_{provider}_{timestamp}.json"
            symbol_value = "global"
        else:
            filename = f"{symbol}_{provider}_{timestamp}.json"
            symbol_value = symbol

        filepath = NEWS_EXPORTS_DIR / filename

        # Standardize article format for export
        export_data = {
            "version": "1.1",
            "export_date": datetime.now().isoformat(),
            "news_type": news_type,
            "symbol": symbol_value,
            "provider": provider,
            "article_count": len(articles),
            "articles": []
        }

        for article in articles:
            # Standardize date format
            date = article.get("date") or article.get("published_at") or ""
            if isinstance(date, datetime):
                date = date.isoformat()

            export_data["articles"].append({
                "title": article.get("title", ""),
                "summary": article.get("summary") or article.get("content", ""),
                "source": article.get("source", ""),
                "url": article.get("url", ""),
                "published_at": date,
                "sentiment": article.get("sentiment"),
                "sentiment_score": article.get("sentiment_score")
            })

        # Write to file
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Exported {len(articles)} articles to {filepath}")

        return {
            "success": True,
            "filename": filename,
            "filepath": str(filepath),
            "article_count": len(articles),
            "message": f"Exported {len(articles)} articles to {filename}"
        }

    except Exception as e:
        logger.error(f"Error exporting news: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export news: {str(e)}"
        )


@router.get("/news/exports")
async def list_news_exports():
    """
    List all exported news files.

    Returns:
        List of export files with metadata
    """
    exports = []

    if NEWS_EXPORTS_DIR.exists():
        for filepath in NEWS_EXPORTS_DIR.glob("*.json"):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                exports.append({
                    "filename": filepath.name,
                    "filepath": str(filepath),
                    "symbol": data.get("symbol", ""),
                    "provider": data.get("provider", ""),
                    "article_count": data.get("article_count", 0),
                    "export_date": data.get("export_date", ""),
                    "size_kb": round(filepath.stat().st_size / 1024, 2)
                })
            except Exception as e:
                logger.warning(f"Error reading export file {filepath}: {e}")

    return {
        "exports": sorted(exports, key=lambda x: x["export_date"], reverse=True),
        "count": len(exports),
        "directory": str(NEWS_EXPORTS_DIR)
    }


@router.get("/fundamentals/providers")
async def list_fundamentals_providers():
    """
    List available fundamentals providers and their capabilities.

    Returns:
        List of providers with their supported features
    """
    import os

    providers = [
        {
            "id": "yfinance",
            "name": "Yahoo Finance",
            "description": "Financial statements from Yahoo Finance (free, no API key required)",
            "requires_api_key": False,
            "api_key_configured": True,
            "available": True,
            "features": ["overview", "balance_sheet", "income_statement", "cashflow_statement", "past_earnings"]
        },
        {
            "id": "fmp",
            "name": "Financial Modeling Prep",
            "description": "Financial statements and company profile from FMP API",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("FMP_API_KEY")),
            "available": bool(os.getenv("FMP_API_KEY")),
            "features": ["overview", "balance_sheet", "income_statement", "cashflow_statement", "past_earnings"]
        },
        {
            "id": "alphavantage",
            "name": "Alpha Vantage",
            "description": "Financial statements and company overview from Alpha Vantage API",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("ALPHA_VANTAGE_API_KEY")),
            "available": bool(os.getenv("ALPHA_VANTAGE_API_KEY")),
            "features": ["overview", "balance_sheet", "income_statement", "cashflow_statement", "past_earnings"]
        }
    ]

    return {
        "providers": providers,
        "data_types": ["overview", "balance_sheet", "income_statement", "cashflow_statement", "past_earnings"],
        "frequencies": ["quarterly", "annual"],
        "default_provider": "yfinance"
    }


@router.get("/fundamentals/fetch")
async def fetch_fundamentals(
    symbol: str = Query(..., description="Stock ticker symbol (e.g., AAPL)"),
    provider: str = Query("yfinance", description="Single provider: yfinance, fmp, alphavantage"),
    providers: Optional[str] = Query(None, description="Comma-separated priority list of providers (e.g., 'yfinance,fmp,alphavantage'). Overrides 'provider'."),
    data_type: str = Query("balance_sheet", description="Data type: overview, balance_sheet, income_statement, cash_flow, earnings"),
    frequency: str = Query("quarterly", description="Frequency: quarterly or annual"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD). Use either this OR lookback_periods."),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD). Defaults to today."),
    lookback_periods: Optional[int] = Query(None, description="Number of periods to look back. Use either this OR start_date. Default: 8"),
    merge: bool = Query(False, description="If true and multiple providers are specified, merge data from all providers (10-day date tolerance). If false, use fallback (first successful provider).")
):
    """
    Fetch fundamental data for a ticker using the ba2_providers.

    Args:
        symbol: Stock ticker symbol
        provider: Single data provider (yfinance, fmp, alphavantage)
        providers: Comma-separated list of providers in priority order (overrides provider param)
        data_type: Type of data (overview, balance_sheet, income_statement, cash_flow, earnings)
        frequency: Data frequency (quarterly or annual)
        start_date: Start date for historical data (YYYY-MM-DD)
        end_date: End date for historical data (YYYY-MM-DD)
        lookback_periods: Number of periods to look back (alternative to start_date)

    Returns:
        Fundamental data with historical periods (normalized format when using multiple providers)
    """
    try:
        # Parse provider list
        provider_list = None
        if providers:
            provider_list = [p.strip() for p in providers.split(",") if p.strip()]
            logger.info(f"Fetching {data_type} for {symbol} using providers {provider_list} ({frequency})")
        else:
            logger.info(f"Fetching {data_type} for {symbol} using {provider} ({frequency})")

        # Parse dates
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            end_dt = datetime.now()

        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        else:
            start_dt = None

        # Default lookback if neither start_date nor lookback_periods provided
        if start_dt is None and lookback_periods is None:
            lookback_periods = 8

        # Handle overview separately (doesn't support multi-provider yet)
        if data_type == "overview":
            return await _fetch_fundamentals_overview(symbol, provider, end_dt)

        # Use FundamentalsService for multi-provider support with priority fallback or merge
        if provider_list and len(provider_list) > 0:
            from ba2_providers.fundamentals.service import FundamentalsService
            service = FundamentalsService(providers=provider_list)

            # Map data_type to service method
            # Support both old names (cashflow_statement, past_earnings) and new names (cash_flow, earnings)
            # Use merged methods if merge=True, otherwise use fallback methods
            if data_type in ("balance_sheet",):
                if merge and len(provider_list) > 1:
                    result = service.get_balance_sheet_merged(
                        symbol=symbol, frequency=frequency, end_date=end_dt,
                        start_date=start_dt, lookback_periods=lookback_periods
                    )
                else:
                    result = service.get_balance_sheet(
                        symbol=symbol, frequency=frequency, end_date=end_dt,
                        start_date=start_dt, lookback_periods=lookback_periods
                    )
            elif data_type in ("income_statement",):
                if merge and len(provider_list) > 1:
                    result = service.get_income_statement_merged(
                        symbol=symbol, frequency=frequency, end_date=end_dt,
                        start_date=start_dt, lookback_periods=lookback_periods
                    )
                else:
                    result = service.get_income_statement(
                        symbol=symbol, frequency=frequency, end_date=end_dt,
                        start_date=start_dt, lookback_periods=lookback_periods
                    )
            elif data_type in ("cash_flow", "cashflow_statement"):
                if merge and len(provider_list) > 1:
                    result = service.get_cash_flow_merged(
                        symbol=symbol, frequency=frequency, end_date=end_dt,
                        start_date=start_dt, lookback_periods=lookback_periods
                    )
                else:
                    result = service.get_cash_flow(
                        symbol=symbol, frequency=frequency, end_date=end_dt,
                        start_date=start_dt, lookback_periods=lookback_periods
                    )
            elif data_type in ("earnings", "past_earnings"):
                result = service.get_earnings(
                    symbol=symbol, frequency=frequency, end_date=end_dt,
                    lookback_periods=lookback_periods or 8
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown data_type: {data_type}. Available: overview, balance_sheet, income_statement, cash_flow, earnings"
                )

            # Return the normalized response
            return result.to_dict()

        # Single provider mode (backwards compatibility)
        if provider == "yfinance":
            from ba2_providers.fundamentals.details import YFinanceCompanyDetailsProvider
            details_provider = YFinanceCompanyDetailsProvider()
        elif provider == "fmp":
            from ba2_providers.fundamentals.details import FMPCompanyDetailsProvider
            details_provider = FMPCompanyDetailsProvider()
        elif provider == "alphavantage":
            from ba2_providers.fundamentals.details import AlphaVantageCompanyDetailsProvider
            details_provider = AlphaVantageCompanyDetailsProvider()
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown provider: {provider}. Available: yfinance, fmp, alphavantage"
            )

        # Fetch the requested data type
        result = None
        try:
            if data_type == "balance_sheet":
                result = details_provider.get_balance_sheet(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_dt,
                    start_date=start_dt,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )
            elif data_type == "income_statement":
                result = details_provider.get_income_statement(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_dt,
                    start_date=start_dt,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )
            elif data_type in ("cash_flow", "cashflow_statement"):
                result = details_provider.get_cashflow_statement(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_dt,
                    start_date=start_dt,
                    lookback_periods=lookback_periods,
                    format_type="dict"
                )
            elif data_type in ("earnings", "past_earnings"):
                result = details_provider.get_past_earnings(
                    symbol=symbol,
                    frequency=frequency,
                    end_date=end_dt,
                    lookback_periods=lookback_periods or 8,
                    format_type="dict"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown data_type: {data_type}. Available: overview, balance_sheet, income_statement, cash_flow, earnings"
                )
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e)
            )

        if result:
            if isinstance(result, dict):
                result["provider"] = provider
                # Different providers use different keys for the data array
                period_count = len(result.get('statements', result.get('earnings', result.get('periods', []))))
                logger.info(f"Fetched {data_type} for {symbol} from {provider}: {period_count} periods")
                return result
            elif isinstance(result, str):
                # Error message from provider
                logger.warning(f"Provider returned error for {symbol}: {result}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=result
                )
        else:
            return {
                "symbol": symbol,
                "provider": provider,
                "data_type": data_type,
                "periods": [],
                "message": "No data available"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching fundamentals: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch fundamentals: {str(e)}"
        )


async def _fetch_fundamentals_overview(symbol: str, provider: str, as_of_date: datetime) -> Dict[str, Any]:
    """
    Fetch company overview from overview providers.
    """
    try:
        if provider == "yfinance":
            # YFinance doesn't have a separate overview provider, use yfinance directly
            import yfinance as yf
            ticker = yf.Ticker(symbol.upper())
            info = ticker.info
            result = {
                "symbol": symbol.upper(),
                "provider": "yfinance",
                "data_type": "overview",
                "retrieved_at": datetime.now().isoformat(),
                "current": {
                    "company_name": info.get("longName", ""),
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "market_cap": info.get("marketCap"),
                    "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                    "trailing_pe": info.get("trailingPE"),
                    "forward_pe": info.get("forwardPE"),
                    "eps": info.get("trailingEps"),
                    "forward_eps": info.get("forwardEps"),
                    "dividend_yield": info.get("dividendYield"),
                    "beta": info.get("beta"),
                    "52_week_high": info.get("fiftyTwoWeekHigh"),
                    "52_week_low": info.get("fiftyTwoWeekLow"),
                    "revenue": info.get("totalRevenue"),
                    "gross_profit": info.get("grossProfits"),
                    "free_cash_flow": info.get("freeCashflow"),
                    "debt_to_equity": info.get("debtToEquity"),
                    "roe": info.get("returnOnEquity"),
                    "roa": info.get("returnOnAssets"),
                    "profit_margin": info.get("profitMargins"),
                    "operating_margin": info.get("operatingMargins"),
                }
            }
            return result
        elif provider == "fmp":
            from ba2_providers.fundamentals.overview import FMPCompanyOverviewProvider
            overview_provider = FMPCompanyOverviewProvider()
            result = overview_provider.get_fundamentals_overview(
                symbol=symbol,
                as_of_date=as_of_date,
                format_type="dict"
            )
        elif provider == "alphavantage":
            from ba2_providers.fundamentals.overview import AlphaVantageCompanyOverviewProvider
            overview_provider = AlphaVantageCompanyOverviewProvider()
            result = overview_provider.get_fundamentals_overview(
                symbol=symbol,
                as_of_date=as_of_date,
                format_type="dict"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown provider: {provider}. Available: yfinance, fmp, alphavantage"
            )

        if isinstance(result, dict):
            result["provider"] = provider
            result["data_type"] = "overview"

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching overview for {symbol}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch overview: {str(e)}"
        )


@router.get("/macro/fetch")
async def fetch_macro(
    indicators: str = Query(..., description="Comma-separated list of indicators (e.g., interest_rate,gdp,inflation)"),
    provider: str = Query("fred", description="Provider: fred (only FRED is currently supported)"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD). Defaults to 1 year ago."),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD). Defaults to today.")
):
    """
    Fetch macroeconomic indicators from FRED.

    Args:
        indicators: Comma-separated list of indicator IDs
        provider: Data provider (currently only 'fred' is supported)
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)

    Returns:
        Macro indicator data with time series
    """
    try:
        from app.services.macro import MacroService

        # Validate provider
        if provider != "fred":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown provider: {provider}. Currently only 'fred' is supported."
            )

        # Parse indicators
        indicator_list = [i.strip() for i in indicators.split(',') if i.strip()]

        if not indicator_list:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At least one indicator is required"
            )

        # Parse dates
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        else:
            end_dt = datetime.now()

        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        else:
            start_dt = end_dt - timedelta(days=365)

        logger.info(f"Fetching macro indicators: {indicator_list} from {provider} ({start_dt.date()} to {end_dt.date()})")

        macro_service = MacroService()
        macro_data = macro_service.get_macro_data(
            indicators=indicator_list,
            start_date=start_dt,
            end_date=end_dt
        )

        # Format response
        result = {
            "provider": provider,
            "start_date": start_dt.strftime("%Y-%m-%d"),
            "end_date": end_dt.strftime("%Y-%m-%d"),
            "indicators": {}
        }

        for indicator, df in macro_data.items():
            indicator_info = macro_service.MACRO_INDICATORS.get(indicator, {})
            if df is not None and not df.empty:
                # Convert DataFrame to list of records
                # Note: DataFrame has 'Date' column and indicator name as value column
                data_records = []
                for _, row in df.iterrows():
                    # Get date from 'Date' column (capital D)
                    date_val = row.get('Date', row.get('date'))
                    # Get value from indicator column (column is renamed to indicator name)
                    value_val = row.get(indicator, row.get('value'))
                    data_records.append({
                        "date": date_val.strftime("%Y-%m-%d") if hasattr(date_val, 'strftime') else str(date_val),
                        "value": float(value_val) if value_val is not None and not pd.isna(value_val) else None
                    })
                result["indicators"][indicator] = {
                    "name": indicator_info.get('name', indicator),
                    "description": indicator_info.get('description', ''),
                    "unit": indicator_info.get('unit', ''),
                    "data": data_records,
                    "count": len(data_records)
                }
                logger.info(f"Fetched {len(data_records)} data points for {indicator}")
            else:
                result["indicators"][indicator] = {
                    "name": indicator_info.get('name', indicator),
                    "description": indicator_info.get('description', ''),
                    "unit": indicator_info.get('unit', ''),
                    "data": [],
                    "count": 0,
                    "error": "No data available"
                }
                logger.warning(f"No data available for {indicator}")

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching macro data: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch macro data: {str(e)}"
        )


@router.get("/maintenance/orphan-models")
async def scan_orphan_models():
    """
    Scan for orphan models - models in trained_models folder that:
    1. Are not saved in the model inventory
    2. Their original job has been deleted

    Returns:
        List of orphan model files with details
    """
    from app.services.job_handler import get_job_models_dir
    from app.api.models import models_store
    from app.api.jobs import jobs_store, load_jobs_from_database

    try:
        # Load jobs from database
        load_jobs_from_database()

        # Get the base trained_models directory
        base_models_dir = MODELS_DIR
        if not base_models_dir.exists():
            return {"orphan_models": [], "total": 0, "total_size_mb": 0}

        orphan_models = []
        total_size = 0

        # Get all model file paths from the inventory
        inventory_paths = set()
        for model in models_store.values():
            if model.get('filePath'):
                inventory_paths.add(Path(model['filePath']).resolve())

        # Scan all job folders
        for job_dir in base_models_dir.iterdir():
            if not job_dir.is_dir():
                continue

            job_id = job_dir.name

            # Check if job exists
            job_exists = job_id in jobs_store

            # Scan model files in this job folder
            for model_file in job_dir.glob("*"):
                if model_file.suffix in ['.pkl', '.pt', '.pth']:
                    # Check if this model is in the inventory
                    in_inventory = model_file.resolve() in inventory_paths

                    if not job_exists and not in_inventory:
                        # This is an orphan model
                        file_size = model_file.stat().st_size
                        total_size += file_size
                        orphan_models.append({
                            "file_path": str(model_file),
                            "file_name": model_file.name,
                            "job_id": job_id,
                            "size_bytes": file_size,
                            "size_mb": round(file_size / (1024 * 1024), 2),
                            "job_exists": job_exists,
                            "in_inventory": in_inventory
                        })

        return {
            "orphan_models": orphan_models,
            "total": len(orphan_models),
            "total_size_mb": round(total_size / (1024 * 1024), 2)
        }

    except Exception as e:
        logger.error(f"Error scanning orphan models: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to scan orphan models: {str(e)}"
        )


@router.delete("/maintenance/orphan-models")
async def cleanup_orphan_models(dry_run: bool = Query(True, description="If True, only report what would be deleted")):
    """
    Clean up orphan models - delete model files that:
    1. Are not saved in the model inventory
    2. Their original job has been deleted

    Args:
        dry_run: If True, only report what would be deleted without actually deleting

    Returns:
        Summary of deleted (or would-be-deleted) files
    """
    from app.services.job_handler import get_job_models_dir
    from app.api.models import models_store
    from app.api.jobs import jobs_store, load_jobs_from_database
    import shutil

    try:
        # Load jobs from database
        load_jobs_from_database()

        # Get the base trained_models directory
        base_models_dir = MODELS_DIR
        if not base_models_dir.exists():
            return {"deleted": [], "total": 0, "total_size_mb": 0, "dry_run": dry_run}

        deleted_items = []
        total_size = 0

        # Get all model file paths from the inventory
        inventory_paths = set()
        for model in models_store.values():
            if model.get('filePath'):
                inventory_paths.add(Path(model['filePath']).resolve())

        # Scan all job folders
        folders_to_remove = []
        for job_dir in base_models_dir.iterdir():
            if not job_dir.is_dir():
                continue

            job_id = job_dir.name
            job_exists = job_id in jobs_store

            # If job doesn't exist, check if any model in this folder is in inventory
            if not job_exists:
                has_inventory_model = False
                for model_file in job_dir.glob("*"):
                    if model_file.resolve() in inventory_paths:
                        has_inventory_model = True
                        break

                if not has_inventory_model:
                    # All files in this folder are orphans
                    folder_size = sum(f.stat().st_size for f in job_dir.rglob("*") if f.is_file())
                    total_size += folder_size
                    deleted_items.append({
                        "path": str(job_dir),
                        "type": "folder",
                        "job_id": job_id,
                        "size_mb": round(folder_size / (1024 * 1024), 2)
                    })
                    folders_to_remove.append(job_dir)

        # Actually delete if not dry run
        if not dry_run:
            for folder in folders_to_remove:
                try:
                    shutil.rmtree(folder)
                    logger.info(f"Deleted orphan model folder: {folder}")
                except Exception as e:
                    logger.error(f"Failed to delete folder {folder}: {e}")

        return {
            "deleted": deleted_items,
            "total": len(deleted_items),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "dry_run": dry_run,
            "message": "Dry run - no files deleted" if dry_run else f"Deleted {len(deleted_items)} orphan model folders"
        }

    except Exception as e:
        logger.error(f"Error cleaning orphan models: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clean orphan models: {str(e)}"
        )


# ============================================================================
# OHLCV Cache Endpoints
# ============================================================================

@router.get("/ohlcv/providers")
async def list_ohlcv_providers():
    """
    List available OHLCV data providers and their configuration status.

    Returns:
        List of OHLCV providers with availability info
    """
    import os

    providers = [
        {
            "id": "yfinance",
            "name": "Yahoo Finance",
            "description": "Free OHLCV data from Yahoo Finance (no API key required)",
            "requires_api_key": False,
            "api_key_configured": True,
            "available": True
        },
        {
            "id": "fmp",
            "name": "Financial Modeling Prep",
            "description": "OHLCV data from FMP API",
            "requires_api_key": True,
            "api_key_configured": bool(os.getenv("FMP_API_KEY")),
            "available": bool(os.getenv("FMP_API_KEY"))
        }
    ]

    return {
        "providers": providers,
        "default": "yfinance"
    }


@router.get("/ohlcv/bars")
def get_ohlcv_bars(symbol: str, start: str, end: str, interval: str = "1d", provider: str = "fmp"):
    """Return OHLCV bars for ONE symbol over [start, end] for charting (e.g. the trade-list
    click-through chart with entry/exit markers). Read-only; uses the cached provider so a
    repeat view is served from disk. Sync def -> FastAPI runs it in a threadpool (the provider
    fetch is blocking).

    Query: symbol, start, end (ISO dates), interval (default 1d), provider (default fmp).
    Returns: {symbol, interval, bars: [{Date, Open, High, Low, Close, Volume}]}.
    """
    from datetime import datetime as _dt
    from app.api.datasets import get_ohlcv_provider

    def _parse(s: str):
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            raise HTTPException(status_code=400, detail=f"invalid ISO date: {s!r}")

    sd, ed = _parse(start), _parse(end)
    try:
        prov = get_ohlcv_provider(provider)
        df = prov.get_ohlcv_data(symbol=symbol.upper(), start_date=sd, end_date=ed, interval=interval)
    except Exception as e:
        logger.error(f"ohlcv/bars fetch failed for {symbol} {interval}: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"OHLCV fetch failed: {e}")
    bars = []
    if df is not None and len(df) > 0:
        for _, row in df.iterrows():
            d = row["Date"]
            bars.append({
                "Date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                "Open": float(row["Open"]), "High": float(row["High"]),
                "Low": float(row["Low"]), "Close": float(row["Close"]),
                "Volume": float(row.get("Volume", 0) or 0),
            })
    return {"symbol": symbol.upper(), "interval": interval, "bars": bars}


@router.post("/ohlcv/fetch-cache")
async def fetch_ohlcv_cache(request: Dict[str, Any]):
    """
    Queue OHLCV cache fetch jobs for multiple symbols and timeframes.

    Each symbol gets its own background task that fetches all requested timeframes.

    Args:
        request: Dict with provider, symbols list, and timeframes list

    Returns:
        List of queued task IDs
    """
    from app.services.task_queue import get_ohlcv_task_queue

    provider = request.get('provider', 'yfinance')
    symbols = [s.strip().upper() for s in request.get('symbols', []) if s.strip()]
    timeframes = request.get('timeframes', ['1d'])
    parallel_jobs = int(request.get('parallel_jobs', 3))
    executor_workers = int(request.get('executor_workers', 5))

    if not symbols:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="symbols list is required and cannot be empty"
        )

    if not timeframes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="timeframes list is required and cannot be empty"
        )

    ohlcv_queue = get_ohlcv_task_queue()
    # Resize the dedicated OHLCV queue to match the requested parallelism
    ohlcv_queue.resize_workers(parallel_jobs)

    task_ids = []
    for symbol in symbols:
        task_id = ohlcv_queue.queue_task(
            task_type='ohlcv_cache_fetch',
            name=f'Cache OHLCV: {symbol}',
            payload={
                'provider': provider,
                'symbol': symbol,
                'timeframes': timeframes,
                'start_date': request.get('start_date'),
                'end_date': request.get('end_date'),
                'executor_workers': executor_workers,
            },
            description=(
                f'Fetch and cache {symbol} OHLCV data '
                f'({", ".join(timeframes)}) — {executor_workers} fetch workers'
            ),
            max_retries=1,
            timeout_seconds=3600,
        )
        task_ids.append({'symbol': symbol, 'task_id': task_id})

    logger.info(
        f"Queued {len(symbols)} OHLCV fetch tasks "
        f"(parallel_jobs={parallel_jobs}, executor_workers={executor_workers})"
    )

    return {
        "task_ids": task_ids,
        "count": len(symbols),
        "provider": provider,
        "timeframes": timeframes,
        "parallel_jobs": parallel_jobs,
        "executor_workers": executor_workers,
    }


def _ohlcv_cache_roots() -> "list[Path]":
    """The native OHLCV cache provider dirs (CACHE_FOLDER/<*OHLCV*Provider>/).

    Single source of truth for the OHLCV cache location — the same dirs cache_manager counts
    and the backtest/live read. Repointed here (was the old CACHE_FOLDER/ohlcv/<short>/ layout)
    after the cache unification. Patch this in tests to control the scan root.
    """
    from app.services.cache_manager import _ohlcv_roots
    return _ohlcv_roots()


def _iter_ohlcv_cache_files() -> "list[tuple[str, Path]]":
    """(provider_dir_name, file) for every native OHLCV cache file (parquet + legacy csv)."""
    out: "list[tuple[str, Path]]" = []
    for root in _ohlcv_cache_roots():
        if not root.exists():
            continue
        for fp in sorted(list(root.glob("*.parquet")) + list(root.glob("*.csv"))):
            out.append((root.name, fp))
    return out


def _read_ohlcv_dates(fp: Path):
    """Sorted, tz-aware, NaT-dropped Date series for a native OHLCV cache file (parquet or csv)."""
    import pandas as pd
    if fp.suffix == ".csv":
        df = pd.read_csv(fp, usecols=['Date'])
    else:
        df = pd.read_parquet(fp, columns=['Date'])
    d = pd.to_datetime(df['Date'], utc=True, errors='coerce').dropna().sort_values()
    return d.reset_index(drop=True)


@router.get("/ohlcv/cache-status")
async def get_ohlcv_cache_status():
    """
    Get information about existing OHLCV cache files.

    Scans the native OHLCV cache (CACHE_FOLDER/<*OHLCV*Provider>/, parquet — the single unified
    cache) and returns per-file metadata.

    Returns:
        List of cache file entries with symbol, interval, size, and modification time
    """
    entries = []
    for provider_name, filepath in _iter_ohlcv_cache_files():
        try:
            name_parts = filepath.stem.rsplit('_', 1)
            if len(name_parts) == 2:
                symbol, interval = name_parts
            else:
                symbol, interval = filepath.stem, "unknown"

            stat = filepath.stat()
            rows = 0
            date_from = None
            date_to = None
            try:
                dates = _read_ohlcv_dates(filepath)
                rows = len(dates)
                if rows:
                    date_from = dates.iloc[0].isoformat()
                    date_to = dates.iloc[-1].isoformat()
            except Exception:
                pass

            entries.append({
                "provider": provider_name,
                "symbol": symbol,
                "interval": interval,
                "file_size": stat.st_size,
                "file_size_mb": round(stat.st_size / (1024 * 1024), 2),
                "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "rows": max(0, rows),
                "date_from": date_from,
                "date_to": date_to,
                "filename": filepath.name,
            })
        except Exception as e:
            logger.warning(f"Error reading cache file {filepath}: {e}")

    entries.sort(key=lambda x: (x['provider'], x['symbol'], x['interval']))
    roots = _ohlcv_cache_roots()
    return {
        "cache_files": entries,
        "count": len(entries),
        "cache_directory": str(roots[0].parent) if roots else "",
    }


@router.get("/ohlcv/check-gaps")
async def check_ohlcv_gaps():
    """
    Analyze all OHLCV cache files for internal data gaps.

    A gap is defined as a time interval between consecutive rows
    that exceeds 5 calendar days (covers weekends + holidays).

    Scans the native OHLCV cache (CACHE_FOLDER/<*OHLCV*Provider>/, parquet — the single unified
    cache).

    Returns:
        Report with gap details per cache file, sorted by gap count descending
    """
    import pandas as pd

    results = []
    for provider_name, filepath in _iter_ohlcv_cache_files():
        try:
            name_parts = filepath.stem.rsplit('_', 1)
            symbol, interval = name_parts if len(name_parts) == 2 else (filepath.stem, "unknown")

            df = _read_ohlcv_dates(filepath).to_frame(name='Date')

            gaps = []
            if len(df) > 1:
                diffs = df['Date'].diff()
                gap_threshold = pd.Timedelta(days=5)
                for idx in diffs[diffs > gap_threshold].index:
                    gap_start = df.loc[idx - 1, 'Date']
                    gap_end = df.loc[idx, 'Date']
                    gap_days = int((gap_end - gap_start).total_seconds() / 86400)
                    gaps.append({
                        "gap_start": gap_start.isoformat(),
                        "gap_end": gap_end.isoformat(),
                        "gap_days": gap_days,
                    })

            results.append({
                "provider": provider_name,
                "symbol": symbol,
                "interval": interval,
                "filename": filepath.name,
                "rows": len(df),
                "date_from": df['Date'].min().isoformat() if not df.empty else None,
                "date_to": df['Date'].max().isoformat() if not df.empty else None,
                "gap_count": len(gaps),
                "gaps": gaps,
                "has_gaps": len(gaps) > 0,
            })
        except Exception as e:
            logger.warning(f"Error checking gaps in {filepath}: {e}")

    # Files with gaps first (descending gap count), then clean files alphabetically
    results.sort(key=lambda x: (-x['gap_count'], x['provider'], x['symbol'], x['interval']))

    files_with_gaps = sum(1 for r in results if r['has_gaps'])
    total_gaps = sum(r['gap_count'] for r in results)

    return {
        "results": results,
        "total_files": len(results),
        "files_with_gaps": files_with_gaps,
        "files_without_gaps": len(results) - files_with_gaps,
        "total_gaps": total_gaps,
    }


@router.post("/news/batch-fetch")
async def batch_fetch_news(request: Dict[str, Any]):
    """
    Queue news batch fetch jobs for multiple symbols.

    Each symbol gets its own background task that fetches articles,
    enriches with webpage content, analyzes sentiment, and caches results.

    Args:
        request: Dict with provider, symbols, start_date, end_date

    Returns:
        List of queued task IDs
    """
    from app.services.task_queue import get_task_queue

    provider = request.get('provider')
    symbols = request.get('symbols', [])
    start_date = request.get('start_date')
    end_date = request.get('end_date')

    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provider is required"
        )
    if not symbols:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="symbols list is required and cannot be empty"
        )
    if not start_date or not end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date and end_date are required (YYYY-MM-DD)"
        )

    task_queue = get_task_queue()
    task_ids = []

    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue

        task_id = task_queue.queue_task(
            task_type='news_batch_fetch',
            name=f'News Batch: {symbol}',
            payload={
                'provider': provider,
                'symbols': [symbol],
                'start_date': start_date,
                'end_date': end_date,
            },
            description=f'Fetch and cache news for {symbol} ({start_date} to {end_date})',
            max_retries=1,
            timeout_seconds=3600
        )
        task_ids.append({'symbol': symbol, 'task_id': task_id})

    logger.info(f"Queued {len(task_ids)} news batch fetch tasks")

    return {
        "task_ids": task_ids,
        "count": len(task_ids),
        "provider": provider,
        "start_date": start_date,
        "end_date": end_date,
    }


@router.get("/news/cache-status")
async def get_news_cache_status():
    """
    Get news cache statistics from the database.

    Returns:
        Article counts by provider and ticker
    """
    from app.services.news_cache import NewsCacheService
    try:
        cache = NewsCacheService()
        stats = cache.get_cache_stats()
        return stats
    except Exception as e:
        logger.error(f"Error getting news cache status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get news cache status: {str(e)}"
        )
