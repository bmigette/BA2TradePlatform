"""Alpaca historical-options provider (the incumbent source).

Wraps the two Alpaca calls the cache builder has always used, behind the vendor-neutral
interface:
  * contract discovery — ``get_option_contracts``, queried for BOTH status=INACTIVE and
    status=ACTIVE. INACTIVE is the load-bearing one: the default ACTIVE-only query returns
    nothing for a historical window (every contract in it has since expired), which is the
    single easiest way to build a silently-empty cache.
  * daily bars — ``get_option_bars``, which accepts a LIST of symbols per request, so
    contracts are batched rather than fetched one round-trip each.

HISTORY FLOOR is a hard 2024-01-18, MEASURED against the live API rather than taken from
the docs: four long-dated contracts that were actively trading well before 2024 (SPY
2024-06-21, SPY 2024-12-20, and the SPY / AAPL 2025-01-17 LEAPs), each requested over a
2022-01-01..2026-07-01 window, all return their first bar on exactly that date. Probes at
2016/2018/2020/2022/2023 return zero bars. docs.alpaca.markets' claim of history "to 2016"
is wrong for this API, and the floor is NOT a subscription limit — Algo Trader Plus (OPRA)
upgrades print quality but buys no extra history. Use ThetaData for a deeper window.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Iterable, Iterator, List, Optional

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)

logger = logging.getLogger(__name__)

# See module docstring — measured, not documented.
_ALPACA_OPTIONS_HISTORY_FLOOR = date(2024, 1, 18)
# Symbols per get_option_bars call; the endpoint paginates internally.
_BARS_BATCH = 200
# Standard OCC root only. Corporate-action ADJUSTED contracts (e.g. "1SPY...") are rejected
# by the bars endpoint and are never on the normal %OTM/DTE selection path anyway.
_STANDARD_OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


class AlpacaOptionsProvider(OptionsDataProviderInterface):
    """Historical options via Alpaca's trading (contracts) + option-data (bars) APIs."""

    name = "alpaca"

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None,
                 feed: Optional[str] = None, paper: bool = False):
        self._key = api_key
        self._secret = api_secret
        # Default None = do NOT send `feed`. VERIFIED against the live API: the option-BARS
        # endpoint rejects it outright — `{"message": "unexpected query parameter(s): feed"}`
        # — while the identical request without it returns bars normally. (feed IS valid on
        # the snapshot/chain/latest endpoints, which is the likely source of the confusion.)
        # Left configurable so a future plan/endpoint that does accept it can opt in.
        self.feed = feed
        # Selects the TradingClient HOST only — contract discovery places no orders either
        # way. It must match the KIND of key supplied: a paper key against the live host
        # (or vice versa) fails with a bare "request is not authorized" (40110000) that
        # reads like a bad secret rather than a host mismatch.
        self.paper = paper
        self._tc: Any = None
        self._dc: Any = None

    # -- clients (lazy so importing the module needs no credentials) -----
    def _clients(self):
        if self._tc is None or self._dc is None:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical.option import OptionHistoricalDataClient
            key, secret = self._resolve_keys()
            self._tc = TradingClient(key, secret, paper=self.paper)
            self._dc = OptionHistoricalDataClient(key, secret)
        return self._tc, self._dc

    def _resolve_keys(self):
        """Same precedence as the existing cache builder so both read one configuration:
        explicit args win, then the MARKET-data creds the codebase documents in .env, then
        the generic names."""
        if self._key and self._secret:
            return self._key, self._secret
        import os
        key = (self._key or os.getenv("ALPACA_MARKET_API_KEY")
               or os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID"))
        secret = (self._secret or os.getenv("ALPACA_MARKET_API_SECRET")
                  or os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_API_SECRET")
                  or os.getenv("APCA_API_SECRET_KEY"))
        if not key or not secret:
            raise ValueError(
                "Alpaca options provider needs credentials: pass api_key/api_secret, or set "
                "ALPACA_MARKET_API_KEY/ALPACA_MARKET_API_SECRET (the names the cache builder "
                "already uses), or the generic ALPACA_API_KEY/ALPACA_SECRET_KEY.")
        return key, secret

    # -- interface ------------------------------------------------------
    def history_floor(self) -> date:
        return _ALPACA_OPTIONS_HISTORY_FLOOR

    def discover_contracts(self, underlying: str, *, expiry_gte: date, expiry_lte: date,
                           strike_min: Optional[float] = None,
                           strike_max: Optional[float] = None,
                           max_contracts: Optional[int] = None) -> List[OptionContractMeta]:
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import AssetStatus

        tc, _ = self._clients()

        def _fetch(status) -> List[Any]:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying.upper()], status=status,
                expiration_date_gte=expiry_gte, expiration_date_lte=expiry_lte,
                strike_price_gte=str(strike_min) if strike_min is not None else None,
                strike_price_lte=str(strike_max) if strike_max is not None else None,
                limit=10000)
            return tc.get_option_contracts(req).option_contracts or []

        merged: dict = {}
        for status in (AssetStatus.INACTIVE, AssetStatus.ACTIVE):
            for c in _fetch(status):
                sym = getattr(c, "symbol", None)
                # INACTIVE is fetched first and wins: for a historical build the expired
                # record is the authoritative one.
                if sym and sym not in merged:
                    merged[sym] = c

        out: List[OptionContractMeta] = []
        for sym, c in merged.items():
            if not _STANDARD_OCC.match(sym):
                continue
            exp = getattr(c, "expiration_date", None)
            strike = getattr(c, "strike_price", None)
            ctype = getattr(c, "type", None)
            if exp is None or strike is None:
                continue
            out.append(OptionContractMeta(
                occ_symbol=sym, underlying=underlying.upper(),
                option_type=str(getattr(ctype, "value", ctype) or "").lower(),
                strike=float(strike),
                expiry=exp if isinstance(exp, date) else date.fromisoformat(str(exp)[:10]),
            ))

        if max_contracts is not None and len(out) > max_contracts:
            if strike_min is not None and strike_max is not None:
                centre = (strike_min + strike_max) / 2.0
            else:
                strikes = sorted(c.strike for c in out)
                centre = strikes[len(strikes) // 2]
            out = sorted(out, key=lambda c: abs(c.strike - centre))[:max_contracts]
        return out

    def fetch_eod_bars(self, contracts: Iterable[OptionContractMeta], *,
                       start: date, end: date) -> Iterator[OptionEodBar]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame

        _, dc = self._clients()
        symbols = [c.occ_symbol for c in contracts]
        if not symbols:
            return

        def _build_request(chunk):
            if not self.feed:
                return OptionBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                                         start=start, end=end)
            # alpaca-py's OptionBarsRequest has no `feed` field and silently drops unknown
            # kwargs, so opting in requires declaring it on a subclass for pydantic to
            # serialize it. See __init__: the bars endpoint currently REJECTS it.
            class _FedOptionBarsRequest(OptionBarsRequest):
                feed: Optional[str] = None

            return _FedOptionBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                                         start=start, end=end, feed=self.feed)

        for i in range(0, len(symbols), _BARS_BATCH):
            chunk = symbols[i:i + _BARS_BATCH]
            try:
                resp = dc.get_option_bars(_build_request(chunk))
            except Exception as e:  # noqa: BLE001 — one bad batch must not kill the build
                logger.warning("Alpaca option bars failed for %d symbol(s) starting %s: %s",
                               len(chunk), chunk[0], e)
                continue
            data = getattr(resp, "data", {}) or {}
            for occ, bars in data.items():
                for b in bars:
                    ts = getattr(b, "timestamp", None)
                    if ts is None:
                        continue
                    yield OptionEodBar(
                        occ_symbol=occ, bar_date=ts.date(),
                        open=float(b.open), high=float(b.high),
                        low=float(b.low), close=float(b.close),
                        volume=int(b.volume) if getattr(b, "volume", None) is not None else None,
                        # Alpaca's bars endpoint carries no quote — the cache synthesizes a
                        # spread downstream. ThetaData supplies real bid/ask here.
                        bid=None, ask=None, open_interest=None,
                    )
