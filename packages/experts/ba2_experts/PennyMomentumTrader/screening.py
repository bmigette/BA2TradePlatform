"""
Screening & triage phases (1, 1b, 1c, 2, 3, 4) for PennyMomentumTrader.

Part of the PennyMomentumTrader package split (EX-4): methods are unchanged,
they were moved verbatim out of __init__.py into focused mixin modules. The
mixin is mixed into PennyMomentumTrader (see __init__.py) and uses
``self`` attributes (settings, logger, trade manager, ...) defined there.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ba2_common.core.models import AnalysisOutput, ExpertInstance, MarketAnalysis
from ba2_common.core.db import add_instance, get_db, get_instance
from ba2_common.core.interfaces.LLMServiceInterface import get_llm_service

from ba2_experts.PennyMomentumTrader.conditions import ConditionEvaluator, get_condition_types_for_llm, validate_condition_set
from ba2_experts.PennyMomentumTrader.prompts import (
    build_deep_triage_prompt,
    build_entry_conditions_prompt,
    build_quick_filter_prompt,
)


class ScreeningPhasesMixin:
    def _phase_1_screen(self, market_analysis: MarketAnalysis) -> List[Dict[str, Any]]:
        """Screen stocks via screener provider, merge gainers, enrich with RVOL, and filter for tradability."""
        self.logger.info("Phase 1: Screening stocks")

        screener_name = self.get_setting_with_interface_default(
            "screener_provider", log_warning=False
        )

        from ba2_providers import get_provider

        screener = get_provider("screener", screener_name)

        # Build filters from settings
        sector_exclude_str = self.get_setting_with_interface_default(
            "scan_sector_exclude", log_warning=False
        )
        sector_exclude = (
            [s.strip() for s in sector_exclude_str.split(",") if s.strip()]
            if sector_exclude_str
            else []
        )

        max_candidates = int(self.get_setting_with_interface_default(
            "max_scan_candidates", log_warning=False
        ))
        min_rvol = float(self.get_setting_with_interface_default(
            "min_relative_volume", log_warning=False
        ))
        include_gainers = self.get_setting_with_interface_default(
            "include_gainers", log_warning=False
        )
        price_min = self.get_setting_with_interface_default("scan_price_min", log_warning=False)
        price_max = self.get_setting_with_interface_default("scan_price_max", log_warning=False)
        mcap_min = self.get_setting_with_interface_default("scan_market_cap_min", log_warning=False)
        mcap_max = self.get_setting_with_interface_default("scan_market_cap_max", log_warning=False)
        float_max = self.get_setting_with_interface_default("scan_float_max", log_warning=False)

        filters = {
            "price_min": price_min,
            "price_max": price_max,
            "volume_min": self.get_setting_with_interface_default(
                "scan_volume_min", log_warning=False
            ),
            "market_cap_min": mcap_min,
            "market_cap_max": mcap_max,
            "sector_exclude": sector_exclude,
        }
        if float_max and float(float_max) > 0:
            filters["float_max"] = float_max

        candidates = screener.screen_stocks(filters)
        self.logger.info(f"Screener returned {len(candidates)} candidates")

        # Track all filtered stocks with reasons
        filtered_stocks: Dict[str, Dict[str, Any]] = {}

        # Merge FMP top gainers into candidate pool
        seen_symbols = {c.get("symbol", "").upper() for c in candidates if c.get("symbol")}
        gainers_count = 0
        if include_gainers:
            try:
                raw_gainers = self._fetch_gainers()
                for g in raw_gainers:
                    sym = (g.get("symbol") or "").upper()
                    if not sym or sym in seen_symbols:
                        continue
                    g_price = g.get("price", 0) or 0
                    # NOTE: The FMP /stock_market/gainers endpoint does NOT return marketCap.
                    # When g_mcap is 0 (unknown), skip the mcap bounds check — the stock
                    # will receive its real market cap from the quote enrichment step below,
                    # and any out-of-range stocks will be caught by the post-enrichment filter.
                    g_mcap = g.get("marketCap", 0) or 0
                    if price_min is not None and g_price < float(price_min):
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "gainer_price_too_low",
                            "details": f"Gainer price ${g_price:.3f} below minimum ${price_min}",
                        }
                        continue
                    if price_max is not None and g_price > float(price_max):
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "gainer_price_too_high",
                            "details": f"Gainer price ${g_price:.3f} above maximum ${price_max}",
                        }
                        continue
                    if mcap_min is not None and g_mcap > 0 and g_mcap < float(mcap_min):
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "gainer_mcap_too_low",
                            "details": f"Gainer market cap ${g_mcap/1e6:.1f}M below minimum ${float(mcap_min)/1e6:.0f}M",
                        }
                        continue
                    if mcap_max is not None and g_mcap > 0 and g_mcap > float(mcap_max):
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "gainer_mcap_too_high",
                            "details": f"Gainer market cap ${g_mcap/1e6:.1f}M above maximum ${float(mcap_max)/1e6:.0f}M",
                        }
                        continue
                    g_sector = (g.get("sector") or "").lower()
                    if sector_exclude and g_sector in [s.lower() for s in sector_exclude]:
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "gainer_sector_excluded",
                            "details": f"Gainer sector '{g_sector}' is in exclusion list",
                        }
                        continue
                    candidates.append({
                        "symbol": sym,
                        "company_name": g.get("name", ""),
                        "price": g_price,
                        "volume": g.get("volume", 0),
                        "market_cap": g_mcap,
                        "sector": g.get("sector", ""),
                        "industry": g.get("industry", ""),
                        "exchange": g.get("exchangeShortName") or g.get("exchange", ""),
                        "_source": "gainers",
                    })
                    seen_symbols.add(sym)
                    gainers_count += 1
                if gainers_count:
                    self.logger.info(f"Merged {gainers_count} FMP top gainers into candidate pool")
            except Exception as e:
                self.logger.warning(f"Failed to fetch FMP gainers: {e}")

        # Exclude symbols where we already hold open positions
        open_position_symbols = {
            pos["symbol"] for pos in self._trade_mgr.get_open_positions()
        }
        if open_position_symbols:
            before = len(candidates)
            for c in candidates:
                sym = c.get("symbol")
                if sym and sym in open_position_symbols:
                    filtered_stocks[sym] = {
                        "phase": "screen",
                        "reason": "already_held",
                        "details": "Symbol already in open positions",
                    }
            candidates = [c for c in candidates if c.get("symbol") not in open_position_symbols]
            self.logger.debug(
                f"Excluded {before - len(candidates)} already-held symbols from screener results"
            )

        # Enrich with RVOL via FMP batch quotes
        all_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
        quotes_map = self._fetch_quotes_chunked(all_symbols) if all_symbols else {}

        for c in candidates:
            sym = c.get("symbol", "").upper()
            quote = quotes_map.get(sym, {})
            volume = quote.get("volume") or c.get("volume") or 0
            avg_vol = quote.get("avgVolume", 0) or 0
            rvol = round(volume / avg_vol, 2) if avg_vol > 0 else 0.0
            c["volume"] = volume
            c["avg_volume"] = avg_vol
            c["rvol"] = rvol
            c["change_percent"] = quote.get("changesPercentage", 0) or 0
            # Update price from quote if available
            q_price = quote.get("price")
            if q_price and q_price > 0:
                c["price"] = q_price
            # Update market_cap from quote — critical for gainers whose mcap was
            # unknown (0) when added from the FMP gainers API
            q_mcap = quote.get("marketCap")
            if q_mcap and q_mcap > 0:
                c["market_cap"] = q_mcap

        # Filter by minimum relative volume
        if min_rvol > 0:
            before = len(candidates)
            for c in candidates:
                if c.get("rvol", 0) < min_rvol:
                    sym = c.get("symbol")
                    if sym:
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "low_rvol",
                            "details": f"RVOL {c.get('rvol', 0):.1f}x below minimum {min_rvol:.1f}x",
                        }
            candidates = [c for c in candidates if c.get("rvol", 0) >= min_rvol]
            dropped = before - len(candidates)
            if dropped:
                self.logger.info(f"Dropped {dropped} candidates below RVOL {min_rvol:.1f}x")

        # Sort by RVOL descending (unusual volume first) and cap at max_scan_candidates
        candidates.sort(key=lambda c: c.get("rvol", 0), reverse=True)
        if len(candidates) > max_candidates:
            self.logger.info(
                f"Capping candidates from {len(candidates)} to top {max_candidates} by RVOL"
            )
            for c in candidates[max_candidates:]:
                sym = c.get("symbol")
                if sym:
                    filtered_stocks[sym] = {
                        "phase": "screen",
                        "reason": "rvol_cap",
                        "details": f"Ranked beyond top {max_candidates} by RVOL (rvol={c.get('rvol', 0):.1f}x)",
                    }
            candidates = candidates[:max_candidates]

        self.logger.info(
            f"Phase 1 after RVOL enrichment: {len(candidates)} candidates "
            f"(top RVOL: {candidates[0].get('rvol', 0):.1f}x)" if candidates else
            f"Phase 1 after RVOL enrichment: 0 candidates"
        )

        # Filter through account to remove untradeable symbols
        if candidates:
            from ba2_common.core.instance_resolver import get_instance_resolver
            account = get_instance_resolver().get_account_instance(self.instance.account_id)
            candidate_symbols = [c["symbol"] for c in candidates if c.get("symbol")]
            if candidate_symbols:
                tradeable = account.symbols_exist(candidate_symbols)
                for c in candidates:
                    sym = c.get("symbol")
                    if sym and not tradeable.get(sym, False):
                        filtered_stocks[sym] = {
                            "phase": "screen",
                            "reason": "not_tradeable",
                            "details": "Symbol not tradeable on broker account",
                        }
                candidates = [
                    c
                    for c in candidates
                    if c.get("symbol") and tradeable.get(c["symbol"], False)
                ]
                self.logger.info(
                    f"{len(candidates)} candidates remain after tradability filter"
                )

                # Queue symbols for the host's instrument auto-adder, if installed.
                # InstrumentAutoAdder is live-platform infra (BA2TradePlatform);
                # ba2_experts must not import it, so it is an optional host hook
                # (set via ba2_experts.set_instrument_auto_adder_hook). Default is
                # None (no-op), e.g. in a backtest.
                tradeable_symbols = [c["symbol"] for c in candidates]
                if tradeable_symbols:
                    try:
                        from ba2_experts import get_instrument_auto_adder_hook

                        hook = get_instrument_auto_adder_hook()
                        if hook:
                            hook(tradeable_symbols)
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to queue instruments for auto-adder: {e}"
                        )

        # Save scan results and filtered stocks
        self._update_state(market_analysis, {
            "scan_results": candidates,
            "filtered_stocks": filtered_stocks,
        })
        self._save_analysis_output(
            market_analysis,
            provider_category="screener",
            provider_name=screener_name,
            name="scan_raw_screener_response",
            output_type="json",
            text=json.dumps(candidates, default=str),
            symbol="PENNY_SCAN",
        )

        if candidates:
            from ba2_common.core.db import log_activity
            from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
            symbols = [c["symbol"] for c in candidates if c.get("symbol")]
            log_activity(
                severity=ActivityLogSeverity.INFO,
                activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                description=f"PennyMomentumTrader screened {len(symbols)} tradeable candidates: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}",
                data={"symbols": symbols, "total": len(symbols)},
                source_expert_id=self.instance.id,
            )

        return candidates

    def _get_previously_monitored_symbols(self, current_market_analysis_id: int) -> set:
        """Return symbols from the most recent prior MarketAnalysis that has monitored data."""
        try:
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .where(MarketAnalysis.id != current_market_analysis_id)
                    .order_by(MarketAnalysis.id.desc())
                    .limit(10)
                )
                for ma in session.exec(statement).all():
                    if ma.state and ma.state.get("monitored_symbols"):
                        return set(ma.state["monitored_symbols"].keys())
        except Exception as e:
            self.logger.warning(f"Failed to load previously monitored symbols: {e}")
        return set()

    def _full_scan_completed_today(self, current_market_analysis_id: int) -> bool:
        """Return True if a full (non-resume) scan was already completed today."""
        try:
            today = datetime.utcnow().date()
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .where(MarketAnalysis.id != current_market_analysis_id)
                    .order_by(MarketAnalysis.id.desc())
                    .limit(10)
                )
                for ma in session.exec(statement).all():
                    if not ma.state or not ma.created_at:
                        continue
                    if ma.created_at.date() != today:
                        break  # records are newest-first; stop at first record from a prior day
                    # deep_triage_results is only written during a full scan
                    if ma.state.get("deep_triage_results") is not None:
                        return True
        except Exception as e:
            self.logger.warning(f"Failed to check today's scan history: {e}")
        return False

    def _get_previous_monitored_data(self, current_market_analysis_id: int) -> Dict[str, Dict[str, Any]]:
        """Return the full monitored_symbols dict from the most recent prior MarketAnalysis.

        Searches through recent analyses (newest first by ID) to find one
        that actually has monitored symbols, since restarts may have created
        intermediate analyses with an empty set.
        """
        try:
            with get_db() as session:
                from sqlmodel import select as sql_select
                statement = (
                    sql_select(MarketAnalysis)
                    .where(MarketAnalysis.expert_instance_id == self.instance.id)
                    .where(MarketAnalysis.id != current_market_analysis_id)
                    .order_by(MarketAnalysis.id.desc())
                    .limit(10)
                )
                for ma in session.exec(statement).all():
                    if not ma.state:
                        continue
                    monitored = ma.state.get("monitored_symbols", {})
                    if monitored:
                        self.logger.debug(
                            f"Found monitored_symbols in MarketAnalysis id={ma.id} "
                            f"({len(monitored)} symbols)"
                        )
                        return dict(monitored)
                    self.logger.debug(
                        f"MarketAnalysis id={ma.id} has no monitored_symbols, checking older"
                    )
        except Exception as e:
            self.logger.warning(f"Failed to load previous monitored data: {e}")
        return {}

    def _filter_by_ohlcv_support(
        self, candidates: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Filter out candidates whose symbols are not supported by the configured OHLCV provider.

        This prevents LLM-discovered or StockTwits symbols (e.g. XRP.X, 401JK.X, crypto tickers)
        that have no OHLCV data from wasting deep triage LLM calls and always failing condition
        checks in Phase 5.

        Returns the filtered list with unsupported symbols removed.
        """
        ohlcv_vendor_list = self.get_setting_with_interface_default(
            "vendor_ohlcv", log_warning=False
        )
        ohlcv_vendor = ohlcv_vendor_list[0] if ohlcv_vendor_list else "yfinance"

        from ba2_providers import get_provider
        ohlcv_provider = get_provider("ohlcv", ohlcv_vendor)

        supported = []
        dropped = []
        for candidate in candidates:
            symbol = candidate.get("symbol")
            if not symbol:
                continue
            try:
                df = ohlcv_provider.get_ohlcv_data(symbol, interval="1d", lookback_days=5)
                if df is not None and not df.empty and len(df) >= 1:
                    supported.append(candidate)
                else:
                    dropped.append(symbol)
            except Exception:
                dropped.append(symbol)

        if dropped:
            self.logger.info(
                f"OHLCV filter: dropped {len(dropped)} unsupported symbols: {dropped}"
            )
        return supported

    def _phase_1b_llm_discovery(
        self, screener_candidates: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Use a websearch LLM to discover additional penny stock candidates."""
        discovery_model = self.get_setting_with_interface_default(
            "discovery_llm", log_warning=False
        )
        count = int(self.get_setting_with_interface_default(
            "max_discovery_candidates", log_warning=False
        ))

        if count <= 0:
            return []

        # Build the known-symbols set: open positions + previously monitored + screener results
        open_position_symbols = {
            pos["symbol"] for pos in self._trade_mgr.get_open_positions()
        }
        prev_monitored = self._get_previously_monitored_symbols(market_analysis.id)
        screener_symbols = {c.get("symbol") for c in screener_candidates if c.get("symbol")}
        all_known = open_position_symbols | prev_monitored | screener_symbols

        price_min = self.get_setting_with_interface_default("scan_price_min", log_warning=False)
        price_max = self.get_setting_with_interface_default("scan_price_max", log_warning=False)
        volume_min = int(self.get_setting_with_interface_default("scan_volume_min", log_warning=False))

        known_list = ", ".join(sorted(all_known)) if all_known else "none"
        prompt = (
            f"You are a penny stock momentum trader scanning for today's top movers.\n\n"
            f"Search the web and find {count} US penny stocks with strong momentum catalysts RIGHT NOW.\n\n"
            f"Criteria:\n"
            f"- Price roughly between ${price_min:.2f} and ${price_max:.2f}\n"
            f"- High trading volume today\n"
            f"- Clear catalyst today (earnings, FDA news, SEC filing, short squeeze, "
            f"unusual options activity, social media buzz, technical breakout)\n"
            f"- US-listed stocks only (NYSE, NASDAQ, OTC)\n\n"
            f"We are ALREADY tracking or holding these symbols — DO NOT include them:\n"
            f"{known_list}\n\n"
            f"IMPORTANT: Do NOT invent or guess prices or volumes. We will fetch real data ourselves.\n\n"
            f"Return ONLY a JSON array (no explanation, no markdown) of exactly {count} objects:\n"
            f'[{{"symbol": "ABCD", "catalyst": "short description", "reason": "why it has momentum"}}]'
        )

        self.logger.debug(
            f"Phase 1b: calling discovery LLM {discovery_model} for {count} extra candidates "
            f"(excluding {len(all_known)} known symbols)"
        )

        try:
            llm = get_llm_service().create_llm(
                discovery_model,
                temperature=0.3,
                expert_instance_id=self.instance.id,
                use_case="PennyMomentum LLM Discovery",
            )
            response = llm.invoke(prompt)
            raw_text = response.content if hasattr(response, "content") else str(response)

            items = self._parse_json_response(raw_text, expected_type=list) or []
            self.logger.info(f"Phase 1b: discovery LLM returned {len(items)} candidates")

            # Normalise and validate each item
            # Collect valid symbols first, then batch-fetch prices
            valid_items: List[Dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                symbol = item.get("symbol", "").strip().upper()
                if not symbol or symbol in all_known:
                    continue
                valid_items.append({"symbol": symbol, **item})

            # Batch-fetch live prices for all discovered symbols
            discovered_symbols = [vi["symbol"] for vi in valid_items]
            live_prices = self._get_live_prices(discovered_symbols) if discovered_symbols else {}

            results: List[Dict[str, Any]] = []
            for item in valid_items:
                symbol = item["symbol"]
                live_price = live_prices.get(symbol)
                if not live_price or live_price <= 0:
                    self.logger.debug(f"Phase 1b: skipping {symbol} (no live price)")
                    continue
                results.append({
                    "symbol": symbol,
                    "price": live_price,
                    "volume": None,
                    "market_cap": None,
                    "sector": None,
                    "industry": None,
                    "exchange": None,
                    "beta": None,
                    "is_actively_trading": True,
                    "company_name": None,
                    "country": "US",
                    "_discovery_catalyst": item.get("catalyst", ""),
                    "_discovery_reason": item.get("reason", ""),
                })

            self._save_analysis_output(
                market_analysis,
                provider_category="llm",
                provider_name=discovery_model,
                name="llm_discovery_response",
                output_type="json",
                text=raw_text,
                symbol="PENNY_SCAN",
                prompt=prompt,
            )
            return results

        except Exception as e:
            self.logger.error(f"Phase 1b: discovery LLM failed: {e}", exc_info=True)
            return []

    def _phase_1c_social_discovery(
        self,
        screener_candidates: List[Dict[str, Any]],
        survivors: List[Dict[str, Any]],
        llm_discovered: List[Dict[str, Any]],
        market_analysis: MarketAnalysis,
    ) -> List[Dict[str, Any]]:
        """Fetch StockTwits trending symbols and return new candidates not already known."""
        use_discovery = self.get_setting_with_interface_default(
            "use_stocktwits_discovery", log_warning=False
        )
        if not use_discovery:
            return []

        oauth_token = self.get_setting_with_interface_default(
            "stocktwits_oauth_token", log_warning=False
        ) or ""  # empty string = unauthenticated public access (still works)

        price_max = float(self.get_setting_with_interface_default(
            "stocktwits_discovery_price_max", log_warning=False
        ))

        # Build set of all already-known symbols to avoid duplicates
        all_known_symbols = (
            {c.get("symbol") for c in screener_candidates if c.get("symbol")}
            | {c.get("symbol") for c in survivors if c.get("symbol")}
            | {c.get("symbol") for c in llm_discovered if c.get("symbol")}
            | {pos["symbol"] for pos in self._trade_mgr.get_open_positions()}
        )

        self.logger.info(
            f"Phase 1c: fetching StockTwits trending symbols (price_max=${price_max:.2f})"
        )

        try:
            from ba2_providers.socialmedia.StockTwitsTrending import StockTwitsTrending

            provider = StockTwitsTrending(
                oauth_token=oauth_token,
                price_max=price_max,
            )
            trending = provider.get_trending_symbols()
            self.logger.info(
                f"Phase 1c: StockTwits returned {len(trending)} symbols under ${price_max:.2f}"
            )
        except Exception as e:
            self.logger.warning(f"Phase 1c: StockTwits fetch failed: {e}")
            return []

        results: List[Dict[str, Any]] = []
        for item in trending:
            symbol = item.get("symbol", "").upper()
            if not symbol or symbol in all_known_symbols:
                continue
            price = item.get("price")
            if not price or price <= 0:
                continue
            results.append({
                "symbol": symbol,
                "price": price,
                "volume": item.get("volume"),
                "market_cap": item.get("market_cap"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "exchange": item.get("exchange"),
                "beta": None,
                "is_actively_trading": True,
                "company_name": item.get("company_name"),
                "country": "US",
                "rvol": item.get("rvol"),
                "change_percent": item.get("change_pct"),
                "_source": "stocktwits_trending",
                "_stocktwits_sources": item.get("sources", []),
                "_stocktwits_watchlist_count": item.get("watchlist_count"),
            })
            all_known_symbols.add(symbol)

        self.logger.info(
            f"Phase 1c: {len(results)} new StockTwits candidates "
            f"(not in screener/survivors/llm-discovered)"
        )
        return results

    def _phase_2_quick_filter(
        self, candidates: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Quick-filter candidates via fast LLM, optionally enriched with StockTwits data."""
        self.logger.info(f"Phase 2: Quick filtering {len(candidates)} candidates")

        if not candidates:
            self.logger.debug("Phase 2: no candidates, skipping LLM call")
            self._update_state(market_analysis, {"quick_filter_survivors": []})
            return []

        # Enrich with StockTwits data if configured
        vendor_social = self.get_setting_with_interface_default(
            "vendor_social", log_warning=False
        )
        if "stocktwits" in (vendor_social or []):
            candidates = self._enrich_with_stocktwits(candidates)

        max_survivors = int(self.get_setting_with_interface_default(
            "max_quick_filter_candidates", log_warning=False
        ))
        prompt = build_quick_filter_prompt(candidates, max_survivors)

        scanning_model = self.get_setting_with_interface_default(
            "scanning_llm", log_warning=False
        )
        self.logger.debug(f"Phase 2: calling LLM {scanning_model} (max_survivors={max_survivors})")
        raw_text: Optional[str] = None
        llm_error: Optional[str] = None
        try:
            llm = get_llm_service().create_llm(
                scanning_model,
                temperature=0.3,
                expert_instance_id=self.instance.id,
                use_case="PennyMomentum Quick Filter",
            )
            response = llm.invoke(prompt)
            raw_text = response.content if hasattr(response, "content") else str(response)
        except Exception as e:
            llm_error = str(e)
            self.logger.error(
                f"Phase 2: LLM call failed ({type(e).__name__}: {e}) — "
                "passing all screener candidates through without LLM filtering",
                exc_info=True,
            )

        # If the LLM call failed entirely, skip filtering and pass everyone through
        if llm_error is not None:
            self._update_state(market_analysis, {
                "quick_filter_survivors": [c.get("symbol") for c in candidates if c.get("symbol")],
                "quick_filter_error": llm_error,
            })
            from ba2_common.core.db import log_activity
            from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
            log_activity(
                severity=ActivityLogSeverity.WARNING,
                activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                description=f"PennyMomentumTrader: Phase 2 LLM failed ({llm_error[:120]}). "
                            f"Passing all {len(candidates)} screener candidates to deep triage.",
                data={"error": llm_error, "candidates": [c.get("symbol") for c in candidates]},
                source_expert_id=self.instance.id,
            )
            return candidates

        # Parse JSON response — new format returns {selected: [...], dropped: [...]}
        parsed = self._parse_json_response(raw_text, expected_type=dict)
        if parsed and "selected" in parsed:
            survivors = parsed.get("selected", [])
            dropped_list = parsed.get("dropped", [])
        else:
            # Fallback: old format (plain list) or parse failure
            survivors = self._parse_json_response(raw_text, expected_type=list) or []
            dropped_list = []
            if not survivors:
                # LLM returned something we can't parse — log raw text for diagnosis
                self.logger.warning(
                    f"Phase 2: LLM response could not be parsed as JSON "
                    f"(first 300 chars): {(raw_text or '')[:300]!r}"
                )

        survivor_symbols = [s["symbol"] for s in survivors if isinstance(s, dict) and "symbol" in s]

        self.logger.info(f"Quick filter kept {len(survivor_symbols)} candidates")

        # Track LLM-dropped stocks with justification
        filtered_stocks = dict(market_analysis.state.get("filtered_stocks", {}))
        survivor_set = set(survivor_symbols)
        for item in dropped_list:
            if isinstance(item, dict) and "symbol" in item:
                sym = item["symbol"]
                filtered_stocks[sym] = {
                    "phase": "quick_filter",
                    "reason": "llm_rejected",
                    "details": item.get("reason", "Dropped by LLM quick filter"),
                }
        # Also catch any candidates not in survivors or dropped (edge case)
        for c in candidates:
            sym = c.get("symbol")
            if sym and sym not in survivor_set and sym not in filtered_stocks:
                filtered_stocks[sym] = {
                    "phase": "quick_filter",
                    "reason": "llm_rejected",
                    "details": "Not selected by LLM quick filter (no specific reason provided)",
                }

        # Save outputs
        self._update_state(market_analysis, {
            "quick_filter_survivors": survivor_symbols,
            "filtered_stocks": filtered_stocks,
        })
        self._save_analysis_output(
            market_analysis,
            provider_category="llm",
            provider_name=scanning_model,
            name="quick_filter_response",
            output_type="json",
            text=raw_text,
            symbol="PENNY_SCAN",
            prompt=prompt,
        )

        # Return full candidate dicts for survivors
        return [c for c in candidates if c.get("symbol") in survivor_set]

    def _triage_one_symbol(
        self,
        candidate: Dict[str, Any],
        deep_model: str,
        min_confidence: int,
    ) -> Optional[Dict[str, Any]]:
        """Gather data and run LLM deep triage for a single symbol.

        Returns a result dict consumed by _phase_3_deep_triage, or None to skip.
        All shared-state mutations (DB writes, list appends) are intentionally
        left to the caller so this method is safe to run in a thread pool.
        """
        symbol = candidate.get("symbol")
        if not symbol:
            return None

        self.logger.debug(f"Phase 3 [{symbol}]: gathering data (news, fundamentals, insider, social)")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(self._gather_news, symbol): "news",
                executor.submit(self._gather_fundamentals, symbol): "fundamentals",
                executor.submit(self._gather_insider, symbol): "insider",
                executor.submit(self._gather_social, symbol): "social",
            }
            gathered = {}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    gathered[key] = future.result()
                except Exception as e:
                    self.logger.warning(f"Phase 3 [{symbol}]: {key} gathering failed: {e}")
                    gathered[key] = f"No {key} data available."

        now_utc = datetime.now(timezone.utc)
        _et_offset = -4 if 3 <= now_utc.month <= 11 else -5
        now_et = now_utc + timedelta(hours=_et_offset)
        tz_label = "EDT" if _et_offset == -4 else "EST"
        is_open = self._is_market_open()
        market_state = "Regular session open (09:30–16:00 ET)" if is_open else (
            "Pre-market" if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30)
            else "After-hours / post-market"
        )
        market_context = (
            f"{market_state}. "
            f"Current time: {now_et.strftime('%Y-%m-%d %H:%M')} {tz_label}."
        )
        cand_price = candidate.get("price")
        cand_chg = candidate.get("change_percent")
        if cand_price is not None:
            sign = "+" if (cand_chg or 0) >= 0 else ""
            chg_str = f" Today's change: {sign}{cand_chg:.1f}%" if cand_chg is not None else ""
            if cand_chg is not None and cand_chg != -100:
                prev_close = cand_price / (1 + cand_chg / 100)
                chg_str += f" (prev close ~${prev_close:.4f})"
            market_context += f" Current price: ${cand_price:.4f}.{chg_str}."

        prompt = build_deep_triage_prompt(
            symbol=symbol,
            news=gathered.get("news", "No news data available."),
            insider=gathered.get("insider", "No insider data available."),
            fundamentals=gathered.get("fundamentals", "No fundamentals data available."),
            social=gathered.get("social", "No social sentiment data available."),
            market_context=market_context,
        )

        try:
            self.logger.debug(f"Phase 3 [{symbol}]: calling LLM {deep_model}")
            llm = get_llm_service().create_llm(
                deep_model,
                temperature=0.3,
                expert_instance_id=self.instance.id,
                use_case="PennyMomentum Deep Triage",
            )
            response = llm.invoke(prompt)
            raw_text = response.content if hasattr(response, "content") else str(response)

            result = self._parse_json_response(raw_text, expected_type=dict)
            if not result:
                self.logger.warning(f"Phase 3 [{symbol}]: failed to parse LLM response")
                return {
                    "symbol": symbol, "candidate": candidate,
                    "confidence": None, "result": None,
                    "raw_text": raw_text, "prompt": prompt,
                    "filter_entry": {"phase": "deep_triage", "reason": "llm_parse_failed",
                                     "details": "Failed to parse LLM deep triage response"},
                }

            confidence = result.get("confidence", 0)
            self.logger.debug(f"Phase 3 [{symbol}]: confidence={confidence}, catalyst={result.get('catalyst', '')!r}")

            if confidence < min_confidence:
                self.logger.info(f"{symbol} confidence {confidence} below threshold {min_confidence}, skipping")
                return {
                    "symbol": symbol, "candidate": candidate,
                    "confidence": confidence, "result": None,
                    "raw_text": raw_text, "prompt": prompt,
                    "filter_entry": {
                        "phase": "deep_triage", "reason": "low_confidence",
                        "details": (
                            f"Confidence {confidence} below threshold {min_confidence}. "
                            f"Catalyst: {result.get('catalyst', 'N/A')}. "
                            f"Risk: {result.get('risk_assessment', 'N/A')}. "
                            f"Reasoning: {result.get('reasoning', 'N/A')}"
                        ),
                    },
                }

            return {
                "symbol": symbol, "candidate": candidate,
                "confidence": confidence, "result": result,
                "raw_text": raw_text, "prompt": prompt,
                "filter_entry": None,
            }

        except Exception as e:
            self.logger.error(f"Deep triage failed for {symbol}: {e}", exc_info=True)
            return {
                "symbol": symbol, "candidate": candidate,
                "confidence": None, "result": None,
                "raw_text": "", "prompt": "",
                "filter_entry": {"phase": "deep_triage", "reason": "deep_triage_error",
                                 "details": f"Deep triage failed: {e}"},
            }

    def _phase_3_deep_triage(
        self, survivors: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ) -> List[Dict[str, Any]]:
        """Deep triage analysis of survivors via analytical LLM."""
        self.logger.info(f"Phase 3: Deep triage of {len(survivors)} survivors")

        if not survivors:
            self._update_state(market_analysis, {"deep_triage_results": {}})
            return []

        max_final = int(self.get_setting_with_interface_default(
            "max_final_candidates", log_warning=False
        ))
        deep_model = self.get_setting_with_interface_default(
            "deep_analysis_llm", log_warning=False
        )
        min_confidence = int(self.get_setting_with_interface_default(
            "min_confidence_threshold", log_warning=False
        ))
        max_workers = int(self.get_setting_with_interface_default(
            "deep_triage_workers", log_warning=False
        ))

        deep_triage_results: Dict[str, Dict[str, Any]] = {}
        finalists: List[Dict[str, Any]] = []
        filtered_stocks = dict(market_analysis.state.get("filtered_stocks", {}))
        total_survivors = len(survivors)

        self.logger.info(f"Phase 3: running deep triage with {max_workers} parallel workers")

        # Submit all symbols; collect results as each worker finishes.
        # All shared-state mutations happen here in the main thread (no locks needed).
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_symbol = {
                executor.submit(self._triage_one_symbol, candidate, deep_model, min_confidence): candidate
                for candidate in survivors
            }

            completed_count = 0
            for future in as_completed(future_to_symbol):
                if self._stop_event.is_set():
                    break

                completed_count += 1
                try:
                    triage = future.result()
                except Exception as e:
                    self.logger.error(f"Deep triage worker raised unexpectedly: {e}", exc_info=True)
                    continue

                if triage is None:
                    continue

                symbol = triage["symbol"]
                self.logger.info(f"Phase 3: {completed_count}/{total_survivors} complete - {symbol}")

                if triage["filter_entry"]:
                    filtered_stocks[symbol] = triage["filter_entry"]
                    self._update_state(market_analysis, {
                        "filtered_stocks": dict(filtered_stocks),
                        "deep_triage_progress": {
                            "current_symbol": symbol,
                            "processed": completed_count,
                            "total": total_survivors,
                            "passed": len(finalists),
                        },
                    })
                    continue

                # Passed confidence threshold — write to DB and update shared state
                result = triage["result"]
                confidence = triage["confidence"]
                candidate = triage["candidate"]
                raw_text = triage["raw_text"]
                prompt = triage["prompt"]

                # No ExpertRecommendation is created (consistency with FactorRanker).
                # The candidate is recorded for the UI/audit via the analysis state
                # (deep_triage_results, below) and the EXPERT_RECOMMENDATION activity log.
                from ba2_common.core.db import log_activity
                from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.SUCCESS,
                    activity_type=ActivityLogType.EXPERT_RECOMMENDATION,
                    description=(
                        f"PennyMomentumTrader recommendation: BUY {symbol} "
                        f"| Confidence: {confidence}% "
                        f"| Catalyst: {result.get('catalyst', 'N/A')} "
                        f"| Expected profit: {result.get('expected_profit_pct', 0):.1f}%"
                    ),
                    data={
                        "symbol": symbol,
                        "confidence": confidence,
                        "catalyst": result.get("catalyst", ""),
                        "strategy": result.get("strategy", ""),
                        "expected_profit_pct": result.get("expected_profit_pct", 0),
                    },
                    source_expert_id=self.instance.id,
                )

                result["price"] = candidate.get("price")
                result["rvol"] = candidate.get("rvol")
                _p = candidate.get("price")
                _chg = candidate.get("change_percent")
                if _p is not None and _chg is not None and _chg != -100:
                    result["prev_close"] = _p / (1 + _chg / 100)
                deep_triage_results[symbol] = result
                finalists.append({
                    "symbol": symbol,
                    "confidence": confidence,
                    "price": candidate.get("price"),
                    "catalyst": result.get("catalyst", ""),
                    "strategy": result.get("strategy", ""),
                })

                self._update_state(market_analysis, {
                    "deep_triage_results": dict(deep_triage_results),
                    "filtered_stocks": dict(filtered_stocks),
                    "deep_triage_progress": {
                        "current_symbol": symbol,
                        "processed": completed_count,
                        "total": total_survivors,
                        "passed": len(finalists),
                    },
                })

                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=deep_model,
                    name=f"deep_triage_{symbol}",
                    output_type="json",
                    text=raw_text,
                    symbol=symbol,
                    prompt=prompt,
                )

        # Cap finalists to max_final by confidence (highest confidence first)
        if len(finalists) > max_final:
            finalists.sort(key=lambda f: f.get("confidence", 0), reverse=True)
            dropped = finalists[max_final:]
            finalists = finalists[:max_final]
            cutoff_confidence = finalists[-1].get("confidence", 0) if finalists else 0
            self.logger.info(
                f"Capped finalists to top {max_final} by confidence "
                f"(dropped: {[f['symbol'] for f in dropped]})"
            )
            # Remove dropped symbols from deep_triage_results to keep state consistent
            for f in dropped:
                sym = f["symbol"]
                deep_triage_results.pop(sym, None)
                filtered_stocks[sym] = {
                    "phase": "deep_triage",
                    "reason": "capped_by_limit",
                    "details": (
                        f"Confidence {f.get('confidence', 0)} passed threshold but "
                        f"ranked beyond top {max_final} finalists (cutoff: {cutoff_confidence})"
                    ),
                }

        # Calculate position sizes for finalists
        if finalists:
            trade_mgr = self._trade_mgr
            available = self.get_available_balance() or 0
            sizing = trade_mgr.calculate_position_sizes(finalists, available)
            for symbol, size_info in sizing.items():
                if symbol in deep_triage_results:
                    deep_triage_results[symbol]["qty"] = size_info["qty"]
                    deep_triage_results[symbol]["allocation"] = size_info["allocation"]

        self._update_state(market_analysis, {
            "deep_triage_results": deep_triage_results,
            "filtered_stocks": filtered_stocks,
        })
        self.logger.info(f"Deep triage produced {len(finalists)} finalists")
        return finalists

    def _phase_4_entry_conditions(
        self, finalists: List[Dict[str, Any]], market_analysis: MarketAnalysis
    ):
        """Generate structured entry/exit conditions for new finalists."""
        self.logger.info(f"Phase 4: Setting entry conditions for {len(finalists)} finalists")

        entry_model = self.get_setting_with_interface_default(
            "entry_definition_llm", log_warning=False
        )
        condition_types_str = get_condition_types_for_llm()

        # Load existing monitored symbols from state; seed from previous run if empty
        # (handles app restart and daily cycle where a fresh MarketAnalysis is created)
        monitored: Dict[str, Dict[str, Any]] = dict(
            market_analysis.state.get("monitored_symbols", {})
        )
        if not monitored:
            prev_data = self._get_previous_monitored_data(market_analysis.id)
            carried = {
                sym: info
                for sym, info in prev_data.items()
                if info.get("status") in ("watching", "triggered")
            }
            if carried:
                self.logger.info(
                    f"Phase 4: carrying over {len(carried)} monitored symbols from previous run: "
                    f"{list(carried.keys())}"
                )
                monitored = carried
            elif prev_data:
                statuses = {}
                for sym, info in prev_data.items():
                    s = info.get("status", "unknown")
                    statuses[s] = statuses.get(s, 0) + 1
                self.logger.warning(
                    f"Phase 4: found {len(prev_data)} symbols in previous analysis "
                    f"but none with status=watching (statuses: {statuses})"
                )
            else:
                self.logger.warning("Phase 4: no previous monitored symbols found in any recent analysis")
        max_monitored = int(self.get_setting_with_interface_default(
            "max_monitored_symbols", log_warning=False
        ))

        # Expire old monitors
        max_age = int(self.get_setting_with_interface_default(
            "max_entry_age_days", log_warning=False
        ))
        now = datetime.now(timezone.utc)
        expired_symbols = []
        for sym, info in monitored.items():
            created_str = info.get("created_at")
            if created_str:
                try:
                    created = datetime.fromisoformat(created_str)
                    if (now - created).days >= max_age and info.get("status") == "watching":
                        expired_symbols.append(sym)
                except (ValueError, TypeError):
                    pass
        for sym in expired_symbols:
            self.logger.info(f"Expiring monitor for {sym} (age exceeded {max_age} days)")
            monitored[sym]["status"] = "expired"

        # Add new finalists
        total_finalists = len(finalists)
        for idx, finalist in enumerate(finalists, 1):
            if self._stop_event.is_set():
                break

            symbol = finalist["symbol"]
            self.logger.info(f"Phase 4: {idx}/{total_finalists} - setting conditions for {symbol}")
            if symbol in monitored and monitored[symbol].get("status") == "watching":
                self.logger.debug(f"{symbol} already being monitored, skipping condition generation")
                continue

            # Check monitor limit
            active_count = sum(
                1 for m in monitored.values() if m.get("status") == "watching"
            )
            if active_count >= max_monitored:
                self.logger.info(
                    f"Monitor limit reached ({max_monitored}), skipping {symbol}"
                )
                continue

            # Build analysis summary for prompt
            deep_triage = market_analysis.state.get("deep_triage_results", {})
            triage_data = deep_triage.get(symbol, {})
            analysis_summary = (
                f"Symbol: {symbol}\n"
                f"Catalyst: {triage_data.get('catalyst', 'N/A')}\n"
                f"Strategy: {triage_data.get('strategy', 'N/A')}\n"
                f"Confidence: {triage_data.get('confidence', 'N/A')}\n"
                f"Expected Profit: {triage_data.get('expected_profit_pct', 'N/A')}%\n"
                f"Risk: {triage_data.get('risk_assessment', 'N/A')}\n"
                f"Reasoning: {triage_data.get('reasoning', 'N/A')}"
            )

            prompt = build_entry_conditions_prompt(
                symbol=symbol,
                analysis_summary=analysis_summary,
                condition_types_str=condition_types_str,
                current_price=triage_data.get("price"),
                current_rvol=triage_data.get("rvol"),
            )

            try:
                llm = get_llm_service().create_llm(
                    entry_model,
                    temperature=0.3,
                    expert_instance_id=self.instance.id,
                    use_case="PennyMomentum Entry Conditions",
                )

                conditions = self._invoke_conditions_with_retry(
                    llm, prompt, symbol=symbol
                )
                if not conditions:
                    self.logger.warning(
                        f"Failed to generate valid entry conditions for {symbol} after retries"
                    )
                    continue

                monitored[symbol] = {
                    "status": "watching",
                    "entry_conditions": conditions.get("entry", {}),
                    "exit_conditions": {
                        "stop_loss": conditions.get("stop_loss", {}),
                        "take_profit": conditions.get("take_profit", []),
                    },
                    "confidence": finalist.get("confidence", 0),
                    "catalyst": finalist.get("catalyst", ""),
                    "strategy": finalist.get("strategy", ""),
                    "qty": triage_data.get("qty"),
                    "allocation": triage_data.get("allocation"),
                    "prev_close": triage_data.get("prev_close"),
                    "created_at": now.isoformat(),
                }

                self._save_analysis_output(
                    market_analysis,
                    provider_category="llm",
                    provider_name=entry_model,
                    name=f"entry_conditions_{symbol}",
                    output_type="json",
                    text=json.dumps(conditions),
                    symbol=symbol,
                    prompt=prompt,
                )

                self.logger.info(f"Set entry conditions for {symbol}")

                from ba2_common.core.db import log_activity
                from ba2_common.core.types import ActivityLogSeverity, ActivityLogType
                log_activity(
                    severity=ActivityLogSeverity.INFO,
                    activity_type=ActivityLogType.ANALYSIS_COMPLETED,
                    description=(
                        f"PennyMomentumTrader watching {symbol}: entry conditions set "
                        f"| Confidence: {finalist.get('confidence', 0)}% "
                        f"| Catalyst: {finalist.get('catalyst', 'N/A')}"
                    ),
                    data={
                        "symbol": symbol,
                        "confidence": finalist.get("confidence", 0),
                        "catalyst": finalist.get("catalyst", ""),
                        "strategy": finalist.get("strategy", ""),
                    },
                    source_expert_id=self.instance.id,
                )

            except Exception as e:
                self.logger.error(
                    f"Failed to set conditions for {symbol}: {e}", exc_info=True
                )

        self._update_state(market_analysis, {"monitored_symbols": monitored})
