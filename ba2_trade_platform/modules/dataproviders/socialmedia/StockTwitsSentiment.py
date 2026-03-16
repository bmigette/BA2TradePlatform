"""
StockTwits Sentiment Data Provider

Fetches real-time social media sentiment from StockTwits using two endpoints:
  1. _next/data  → symbol metadata (watchlist count, sector, price, trending score)
  2. Official API v2 stream → per-message Bullish/Bearish sentiment tags

Cloudflare bot protection is bypassed via curl_cffi browser TLS impersonation.
No authentication required. Rate limit: ~200 req/h on the API v2 stream.
"""

import re
import json
from typing import Dict, Any, Literal, Annotated
from datetime import datetime

from ba2_trade_platform.core.interfaces import SocialMediaDataProviderInterface
from ba2_trade_platform.core.provider_utils import log_provider_call
from ba2_trade_platform.logger import logger


BASE_URL = "https://stocktwits.com"
API_V2_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

_PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
}

_JSON_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-nextjs-data": "1",
}


class StockTwitsSentiment(SocialMediaDataProviderInterface):
    """
    StockTwits sentiment provider using the public website API.

    Combines:
    - Symbol metadata (watchlist count, sector, industry, price, trending score)
      from the Next.js _next/data endpoint
    - Message stream with per-message Bullish/Bearish tags from the official API v2

    Requires curl_cffi (``pip install curl_cffi``) for Cloudflare bypass.
    """

    def __init__(self, message_limit: int = 30):
        """
        Args:
            message_limit: Max messages to fetch from the stream (1-30, API v2 cap).
        """
        super().__init__()
        try:
            from curl_cffi import requests as cf_requests
        except ImportError as exc:
            raise ImportError(
                "curl_cffi is required for StockTwitsSentiment. "
                "Install it with: pip install curl_cffi"
            ) from exc

        self._message_limit = min(max(1, message_limit), 30)
        self._session = cf_requests.Session(impersonate="chrome124")
        self._build_id: str | None = None
        logger.debug(f"Initialized StockTwitsSentiment (message_limit={self._message_limit})")

    # ------------------------------------------------------------------
    # Build ID (lazy, cached per instance)
    # ------------------------------------------------------------------

    def _get_build_id(self) -> str:
        """Fetch the Next.js build ID from a StockTwits symbol page (cached)."""
        if self._build_id:
            return self._build_id

        url = f"{BASE_URL}/symbol/AAPL"
        logger.debug(f"Fetching StockTwits build ID from {url}")
        resp = self._session.get(url, headers=_PAGE_HEADERS, timeout=20)
        resp.raise_for_status()

        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL
        )
        if not match:
            raise RuntimeError("Could not find __NEXT_DATA__ in StockTwits symbol page")

        next_data = json.loads(match.group(1))
        build_id = next_data.get("buildId")
        if not build_id:
            raise RuntimeError("buildId not found in __NEXT_DATA__")

        self._build_id = build_id
        logger.debug(f"StockTwits build ID: {build_id}")
        return build_id

    def invalidate_build_id(self):
        """Force a refresh of the cached build ID on next request."""
        self._build_id = None

    # ------------------------------------------------------------------
    # Internal fetch
    # ------------------------------------------------------------------

    def _fetch_metadata(self, symbol: str) -> dict:
        """
        Fetch symbol metadata from the _next/data endpoint.

        Returns a normalised dict or raises on HTTP error.
        """
        build_id = self._get_build_id()
        url = f"{BASE_URL}/_next/data/{build_id}/symbol/{symbol}.json"
        headers = {**_JSON_HEADERS, "referer": f"{BASE_URL}/symbol/{symbol}"}
        resp = self._session.get(url, params={"symbol": symbol}, headers=headers, timeout=15)

        if resp.status_code == 404:
            # Build ID may have changed (Next.js redeployment) — refresh and retry once
            logger.debug("Got 404 on _next/data — refreshing build ID and retrying")
            self.invalidate_build_id()
            build_id = self._get_build_id()
            url = f"{BASE_URL}/_next/data/{build_id}/symbol/{symbol}.json"
            resp = self._session.get(url, params={"symbol": symbol}, headers=headers, timeout=15)

        resp.raise_for_status()

        page_props = resp.json().get("pageProps", {})
        d = page_props.get("initialData", {})
        pd = d.get("price_data") or {}

        return {
            "company_name": d.get("title"),
            "exchange": d.get("exchange"),
            "sector": d.get("sector"),
            "industry": d.get("industry"),
            "watchlist_count": d.get("watchlistCount"),
            "instrument_class": d.get("instrumentClass"),
            "trending": d.get("trending"),
            "trending_score": d.get("trendingScore"),
            "logo_url": d.get("logoUrl"),
            "price": {
                "last": pd.get("last"),
                "open": pd.get("open"),
                "high": pd.get("high"),
                "low": pd.get("low"),
                "volume": pd.get("volume"),
                "change": pd.get("change"),
                "percent_change": pd.get("percent_change"),
                "previous_close": pd.get("previous_close"),
                "timestamp": pd.get("timestamp"),
            },
        }

    def _fetch_stream(self, symbol: str) -> dict:
        """
        Fetch message stream from the official StockTwits API v2.

        Returns sentiment counts + sample messages dict.
        """
        url = API_V2_STREAM.format(symbol=symbol)
        resp = self._session.get(url, params={"limit": self._message_limit}, timeout=15)
        resp.raise_for_status()

        messages = resp.json().get("messages", [])

        bullish = 0
        bearish = 0
        no_tag = 0
        samples = []

        for msg in messages:
            entities = msg.get("entities") or {}
            sent = entities.get("sentiment")
            basic = sent.get("basic") if sent else None

            if basic == "Bullish":
                bullish += 1
            elif basic == "Bearish":
                bearish += 1
            else:
                no_tag += 1

            if len(samples) < 5:
                samples.append({
                    "id": msg.get("id"),
                    "body": msg.get("body", ""),
                    "created_at": msg.get("created_at"),
                    "sentiment": basic,
                    "likes": (msg.get("likes") or {}).get("total", 0),
                    "username": (msg.get("user") or {}).get("username"),
                })

        tagged = bullish + bearish
        return {
            "bullish": bullish,
            "bearish": bearish,
            "no_sentiment_tag": no_tag,
            "total_messages": len(messages),
            "bullish_pct": round(bullish / tagged * 100, 1) if tagged else None,
            "bearish_pct": round(bearish / tagged * 100, 1) if tagged else None,
            "sample_messages": samples,
        }

    # ------------------------------------------------------------------
    # Main interface method
    # ------------------------------------------------------------------

    @log_provider_call
    def get_social_media_sentiment(
        self,
        symbol: Annotated[str, "Stock ticker symbol"],
        end_date: Annotated[datetime, "End date for sentiment analysis"],
        lookback_days: Annotated[int, "Number of days to look back for sentiment data"],
        format_type: Literal["dict", "markdown", "both"] = "markdown",
    ) -> Dict[str, Any] | str:
        """
        Get StockTwits sentiment for a symbol.

        Note: The StockTwits stream always returns the most recent messages
        regardless of lookback_days — historical pagination is not available
        on the public unauthenticated endpoint. lookback_days is noted in the
        output for context.

        Args:
            symbol: Stock ticker symbol
            end_date: Analysis end date (for output labelling)
            lookback_days: Intended lookback window (informational)
            format_type: 'dict', 'markdown', or 'both'

        Returns:
            Sentiment analysis in the requested format
        """
        symbol = symbol.upper()
        logger.debug(f"StockTwits: fetching sentiment for {symbol}")

        metadata = self._fetch_metadata(symbol)
        stream = self._fetch_stream(symbol)

        logger.debug(
            f"StockTwits [{symbol}]: watchlist={metadata.get('watchlist_count')} "
            f"bull={stream['bullish']} bear={stream['bearish']} "
            f"msgs={stream['total_messages']}"
        )

        # Derive a simple overall sentiment label
        bull_pct = stream.get("bullish_pct")
        bear_pct = stream.get("bearish_pct")
        if bull_pct is None:
            overall = "neutral"
            score = 0.0
        elif bull_pct >= 65:
            overall = "bullish"
            score = round(bull_pct / 100, 3)
        elif bear_pct >= 65:
            overall = "bearish"
            score = round(-bear_pct / 100, 3)
        else:
            overall = "neutral"
            score = round((bull_pct - bear_pct) / 100, 3) if bull_pct is not None else 0.0

        dict_response = {
            "symbol": symbol,
            "source": "StockTwits",
            "analysis_period": {
                "end_date": end_date.isoformat(),
                "lookback_days": lookback_days,
            },
            "metadata": metadata,
            "sentiment": {
                "overall": overall,
                "score": score,
                "bullish": stream["bullish"],
                "bearish": stream["bearish"],
                "no_sentiment_tag": stream["no_sentiment_tag"],
                "total_messages_sampled": stream["total_messages"],
                "bullish_pct": bull_pct,
                "bearish_pct": bear_pct,
            },
            "sample_messages": stream["sample_messages"],
            "timestamp": datetime.now().isoformat(),
        }

        if format_type == "dict":
            return dict_response

        markdown = self._build_markdown(dict_response)

        if format_type == "both":
            return {"text": markdown, "data": dict_response}

        return markdown

    def _build_markdown(self, d: dict) -> str:
        sym = d["symbol"]
        meta = d["metadata"]
        sent = d["sentiment"]
        price = meta.get("price", {})

        lines = [
            f"# StockTwits Sentiment: {sym}",
            f"**Company:** {meta.get('company_name', 'N/A')} ({meta.get('exchange', 'N/A')})",
            f"**Sector / Industry:** {meta.get('sector', 'N/A')} / {meta.get('industry', 'N/A')}",
            f"**Watchlist Count:** {meta.get('watchlist_count', 'N/A'):,}" if meta.get('watchlist_count') else f"**Watchlist Count:** N/A",
            f"**Trending:** {'Yes' if meta.get('trending') else 'No'} (score: {meta.get('trending_score', 'N/A')})",
            "",
        ]

        if price.get("last"):
            lines += [
                "## Price Snapshot",
                f"**Last:** ${price['last']}  |  **Change:** {price.get('change', 0):+.2f} ({price.get('percent_change', 0):+.2f}%)",
                f"**Open:** ${price.get('open', 'N/A')}  |  **High:** ${price.get('high', 'N/A')}  |  **Low:** ${price.get('low', 'N/A')}",
                f"**Volume:** {price.get('volume', 'N/A'):,}" if price.get('volume') else "**Volume:** N/A",
                "",
            ]

        lines += [
            "## Sentiment Analysis",
            f"**Overall:** {sent['overall'].upper()}  (score: {sent['score']:+.3f})",
            f"**Bullish:** {sent['bullish']} messages ({sent['bullish_pct']}%)" if sent.get('bullish_pct') is not None else f"**Bullish:** {sent['bullish']}",
            f"**Bearish:** {sent['bearish']} messages ({sent['bearish_pct']}%)" if sent.get('bearish_pct') is not None else f"**Bearish:** {sent['bearish']}",
            f"**No sentiment tag:** {sent['no_sentiment_tag']}",
            f"**Total messages sampled:** {sent['total_messages_sampled']}",
            "",
            "## Sample Messages",
        ]

        for msg in d.get("sample_messages", []):
            tag = f"[{msg['sentiment']}]" if msg["sentiment"] else "[no tag]"
            user = msg.get("username", "?")
            created = (msg.get("created_at") or "")[:10]
            likes = msg.get("likes", 0)
            body = (msg.get("body") or "").replace("\n", " ")[:200]
            lines.append(f"- **@{user}** {tag} ({created}, {likes} likes): {body}")

        lines += [
            "",
            f"*Source: StockTwits | Fetched: {d['timestamp'][:19]}*",
        ]

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Interface boilerplate
    # ------------------------------------------------------------------

    def get_provider_name(self) -> str:
        return "stocktwits"

    def get_supported_features(self) -> list[str]:
        return ["social_media_sentiment", "sentiment_analysis", "stocktwits"]

    def validate_config(self) -> bool:
        try:
            import curl_cffi  # noqa: F401
            return True
        except ImportError:
            return False

    def _format_as_dict(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, dict):
            return data
        return {"data": data}

    def _format_as_markdown(self, data: Any) -> str:
        if isinstance(data, dict):
            return self._build_markdown(data)
        return str(data)
