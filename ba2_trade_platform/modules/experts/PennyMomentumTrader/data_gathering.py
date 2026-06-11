"""
Market/news/social data gathering and FMP quote plumbing for PennyMomentumTrader.

Part of the PennyMomentumTrader package split (EX-4): methods are unchanged,
they were moved verbatim out of __init__.py into focused mixin modules. The
mixin is mixed into PennyMomentumTrader (see __init__.py) and uses
``self`` attributes (settings, logger, trade manager, ...) defined there.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ....core.ModelFactory import ModelFactory


class DataGatheringMixin:
    def _fetch_gainers(self) -> List[Dict[str, Any]]:
        """Fetch today's top gainers from FMP /api/v3/stock_market/gainers."""
        import requests
        from ....config import get_app_setting

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            return []
        resp = requests.get(
            "https://financialmodelingprep.com/api/v3/stock_market/gainers",
            params={"apikey": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def _fetch_quotes_chunked(
        self, symbols: List[str], chunk_size: int = 50
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch FMP full quotes in chunks. Returns {symbol: quote_dict}.

        Delegates to StockScreener._fetch_quotes_chunked for code reuse.
        """
        from ....core.StockScreener import StockScreener
        return StockScreener._fetch_quotes_chunked(symbols, chunk_size)

    # ------------------------------------------------------------------
    # Live price helpers
    # ------------------------------------------------------------------

    def _get_live_prices(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        """
        Batch-fetch live prices for the given symbols using the configured
        vendor_live_price source.

        Returns:
            Dict mapping symbol -> price (None if unavailable).
        """
        if not symbols:
            return {}

        source = self.get_setting_with_interface_default(
            "vendor_live_price", log_warning=False
        )

        if source == "fmp":
            return self._get_fmp_quotes(symbols)

        # Fallback: use account (broker) API
        from ....core.utils import get_account_instance_from_id
        account = get_account_instance_from_id(self.instance.account_id)
        return account.get_instrument_current_price(symbols, price_type="mid")

    def _is_regular_session(self) -> bool:
        """Return True if current time is within regular market hours (9:30-16:00 ET)."""
        now = self._get_market_now()
        regular_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        regular_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return regular_open <= now <= regular_close

    def _get_fmp_quotes(self, symbols: List[str]) -> Dict[str, Optional[float]]:
        """
        Fetch real-time quotes from FMP.

        During regular market hours (9:30-16:00 ET): uses fmpsdk.quote()
        which returns the current price in a single batch call.

        During extended hours (pre-market / after-hours), tries in order:
          1. /stable/batch-aftermarket-quote  (one call, Premium+ plans)
          2. /stable/aftermarket-quote         (per-symbol, Starter+ plans)
          3. fmpsdk.quote()                    (final fallback)
        """
        import fmpsdk
        from ....config import get_app_setting

        api_key = get_app_setting("FMP_API_KEY")
        if not api_key:
            self.logger.warning("FMP_API_KEY not configured, falling back to account prices")
            from ....core.utils import get_account_instance_from_id
            account = get_account_instance_from_id(self.instance.account_id)
            return account.get_instrument_current_price(symbols, price_type="mid")

        result: Dict[str, Optional[float]] = {s: None for s in symbols}

        try:
            if not self._is_regular_session():
                result = self._get_fmp_aftermarket_quotes(symbols, api_key)
                if any(v is not None for v in result.values()):
                    return result
                self.logger.warning(
                    "FMP aftermarket quotes unavailable during extended hours, "
                    "falling back to regular quote (prices may be stale closing prices)"
                )

            # Regular session or aftermarket fallback: use fmpsdk.quote()
            data = fmpsdk.quote(apikey=api_key, symbol=symbols)
            if isinstance(data, list):
                for item in data:
                    sym = item.get("symbol", "").upper()
                    price = item.get("price")
                    if sym in result and price is not None and price > 0:
                        result[sym] = float(price)
        except Exception as e:
            self.logger.warning(f"FMP quote fetch failed: {e}")

        return result

    def _get_fmp_aftermarket_quotes(
        self, symbols: List[str], api_key: str
    ) -> Dict[str, Optional[float]]:
        """
        Fetch extended-hours quotes from FMP aftermarket endpoints.

        Tries batch endpoint first (/stable/batch-aftermarket-quote),
        falls back to single-symbol endpoint (/stable/aftermarket-quote)
        if the batch returns 402 (plan limitation).

        Returns mid-price computed from bid/ask.
        Returns all-None dict if both endpoints are unavailable.
        """
        import requests

        result: Dict[str, Optional[float]] = {s: None for s in symbols}

        # 1. Try batch endpoint (Premium+ plans)
        try:
            resp = requests.get(
                "https://financialmodelingprep.com/stable/batch-aftermarket-quote",
                params={"symbol": ",".join(symbols), "apikey": api_key},
                timeout=10,
            )
            if resp.status_code != 402:
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        sym = item.get("symbol", "").upper()
                        price = self._extract_aftermarket_price(item)
                        if sym in result and price is not None:
                            result[sym] = price
                    return result
            # 402 = plan doesn't include batch, fall through to single
            self.logger.debug("FMP batch-aftermarket-quote not available (402), trying single endpoint")
        except Exception as e:
            self.logger.debug(f"FMP batch aftermarket failed: {e}")

        # 2. Fall back to single-symbol endpoint (Starter+ plans)
        for symbol in symbols:
            try:
                resp = requests.get(
                    "https://financialmodelingprep.com/stable/aftermarket-quote",
                    params={"symbol": symbol, "apikey": api_key},
                    timeout=10,
                )
                if resp.status_code == 402:
                    self.logger.debug("FMP aftermarket-quote not available (402), falling back to regular quote")
                    return {s: None for s in symbols}
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    price = self._extract_aftermarket_price(data[0])
                    if price is not None:
                        result[symbol] = price
            except Exception as e:
                self.logger.debug(f"FMP aftermarket quote failed for {symbol}: {e}")

        return result

    @staticmethod
    def _extract_aftermarket_price(item: Dict[str, Any]) -> Optional[float]:
        """Compute mid-price from aftermarket bid/ask, or use price field."""
        bid = item.get("bidPrice")
        ask = item.get("askPrice")
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            return round((float(bid) + float(ask)) / 2, 4)
        if bid and float(bid) > 0:
            return float(bid)
        if ask and float(ask) > 0:
            return float(ask)
        # Some responses may include a direct price field
        price = item.get("price")
        if price and float(price) > 0:
            return float(price)
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _gather_news(self, symbol: str) -> str:
        """Aggregate news from all configured news vendors, enriched with full article content."""
        vendor_list = self.get_setting_with_interface_default(
            "vendor_news", log_warning=False
        )
        from ....modules.dataproviders import get_provider
        from ....core.interfaces.MarketNewsInterface import MarketNewsInterface

        all_articles = []
        all_news_markdown: List[str] = []

        for vendor_name in vendor_list:
            try:
                kwargs = {}
                if vendor_name == "ai":
                    kwargs["model"] = self.get_setting_with_interface_default(
                        "websearch_llm", log_warning=False
                    )
                provider = get_provider("news", vendor_name, **kwargs)
                result = provider.get_company_news(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=3,
                    format_type="both",
                )
                if isinstance(result, dict) and "data" in result:
                    articles = result["data"].get("articles", [])
                    all_articles.extend(articles)
                elif isinstance(result, str):
                    all_news_markdown.append(f"--- {vendor_name} ---\n{result}")
            except Exception as e:
                self.logger.warning(
                    f"News provider {vendor_name} failed for {symbol}: {e}"
                )

        # Enrich articles with full content via interface-level enrichment
        if all_articles:
            MarketNewsInterface.enrich_news_result({"articles": all_articles})
            all_news_markdown.append(
                MarketNewsInterface.rebuild_markdown_from_articles(
                    all_articles, heading=f"News for {symbol}"
                )
            )

        return "\n\n---\n\n".join(all_news_markdown) if all_news_markdown else "No news data available."

    def _gather_fundamentals(self, symbol: str) -> str:
        """Get fundamentals from configured vendors."""
        vendor_list = self.get_setting_with_interface_default(
            "vendor_fundamentals", log_warning=False
        )
        from ....modules.dataproviders import get_provider

        for vendor_name in vendor_list:
            try:
                kwargs = {}
                if vendor_name == "ai":
                    kwargs["model"] = self.get_setting_with_interface_default(
                        "websearch_llm", log_warning=False
                    )
                provider = get_provider("fundamentals_overview", vendor_name, **kwargs)
                return provider.get_fundamentals_overview(
                    symbol,
                    as_of_date=datetime.now(timezone.utc),
                    format_type="markdown",
                )
            except Exception as e:
                self.logger.warning(
                    f"Fundamentals provider {vendor_name} failed for {symbol}: {e}"
                )
        return "No fundamentals data available."

    def _gather_insider(self, symbol: str) -> str:
        """Get insider trading data from configured vendors."""
        vendor_list = self.get_setting_with_interface_default(
            "vendor_insider", log_warning=False
        )
        from ....modules.dataproviders import get_provider

        for vendor_name in vendor_list:
            try:
                provider = get_provider("insider", vendor_name)
                return provider.get_insider_transactions(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=90,
                    format_type="markdown",
                )
            except Exception as e:
                self.logger.warning(
                    f"Insider provider {vendor_name} failed for {symbol}: {e}"
                )
        return "No insider data available."

    def _gather_social(self, symbol: str) -> str:
        """Get social sentiment from configured vendors (StockTwits and/or websearch LLM)."""
        vendor_social = self.get_setting_with_interface_default(
            "vendor_social", log_warning=False
        ) or []

        parts: List[str] = []

        if "stocktwits" in vendor_social:
            try:
                provider = self._get_stocktwits_provider()
                result = provider.get_social_media_sentiment(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=3,
                    format_type="markdown",
                )
                parts.append(f"--- StockTwits ---\n{result}")
            except Exception as e:
                self.logger.warning(f"StockTwits failed for {symbol}: {e}")

        if "websearch" in vendor_social:
            websearch_model = self.get_setting_with_interface_default(
                "websearch_llm", log_warning=False
            )
            try:
                llm = ModelFactory.create_llm(
                    websearch_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Social Sentiment",
                )
                prompt = (
                    f"Search for the latest social media sentiment and discussion "
                    f"about {symbol} stock on Reddit, StockTwits, Twitter/X, and "
                    f"financial forums. Summarize the overall sentiment (bullish, "
                    f"bearish, neutral), key themes being discussed, and any "
                    f"notable trends in retail interest. Be concise."
                )
                response = llm.invoke(prompt)
                text = response.content if hasattr(response, "content") else str(response)
                parts.append(f"--- AI Websearch ---\n{text}")
            except Exception as e:
                self.logger.warning(f"Websearch social sentiment failed for {symbol}: {e}")

        return "\n\n".join(parts) if parts else "No social sentiment data available."

    def _get_stocktwits_provider(self):
        """Return a cached StockTwits provider instance (lazy init)."""
        if not hasattr(self, "_stocktwits_provider") or self._stocktwits_provider is None:
            from ....modules.dataproviders.socialmedia import StockTwitsSentiment
            self._stocktwits_provider = StockTwitsSentiment(message_limit=30)
        return self._stocktwits_provider

    def _enrich_with_stocktwits(
        self, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Batch-fetch StockTwits metadata for all candidates and inject fields:
        st_watchlist, st_bullish_pct, st_bearish_pct, st_trending, st_trending_score.

        On failure the candidate is included unenriched so the pipeline continues.
        """
        provider = self._get_stocktwits_provider()
        self.logger.info(
            f"Fetching StockTwits data for {len(candidates)} candidates..."
        )

        enriched = []
        total_candidates = len(candidates)
        for i, candidate in enumerate(candidates):
            symbol = candidate.get("symbol")
            if not symbol:
                enriched.append(candidate)
                continue
            self.logger.debug(f"Phase 2: StockTwits {i + 1}/{total_candidates} - {symbol}")
            try:
                result = provider.get_social_media_sentiment(
                    symbol,
                    end_date=datetime.now(timezone.utc),
                    lookback_days=1,
                    format_type="dict",
                )
                meta = result.get("metadata", {})
                sent = result.get("sentiment", {})
                enriched_candidate = dict(candidate)
                enriched_candidate["st_watchlist"] = meta.get("watchlist_count")
                enriched_candidate["st_bullish_pct"] = sent.get("bullish_pct")
                enriched_candidate["st_bearish_pct"] = sent.get("bearish_pct")
                enriched_candidate["st_trending"] = meta.get("trending")
                enriched_candidate["st_trending_score"] = meta.get("trending_score")
                enriched.append(enriched_candidate)
            except Exception as e:
                self.logger.warning(
                    f"StockTwits enrichment failed for {symbol}: {e}"
                )
                enriched.append(candidate)

            # Small delay between requests to avoid rate limiting
            if i < len(candidates) - 1:
                time.sleep(0.2)

        fetched = sum(1 for c in enriched if c.get("st_watchlist") is not None)
        failed = len(candidates) - fetched
        if failed:
            self.logger.warning(
                f"StockTwits enrichment: {fetched}/{len(candidates)} fetched, "
                f"{failed} failed (will proceed without their social data)"
            )
        else:
            self.logger.info(
                f"StockTwits enrichment complete: {fetched}/{len(candidates)} fetched"
            )
        return enriched
