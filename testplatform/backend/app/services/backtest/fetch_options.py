"""Builds the offline HISTORICAL options cache from Alpaca. CLI: `ba2-test fetch-options`.
Run with the editable venv (~/ba2-venvs/test) which has alpaca-py installed.

WHY a metadata-driven historical cache (NOT the chain snapshot):
  Alpaca's option CHAIN endpoint (`get_option_chain`) is a CURRENT snapshot only — it has
  no as-of greeks / IV / OI for a past date, and it does NOT return EXPIRED contracts. So
  contract METADATA (occ/type/strike/expiry) comes from contract discovery, not the chain
  snapshot.

  IV/GREEKS ARE COMPUTED, NOT FETCHED: IV isn't an independently-observed quantity a vendor
  uniquely holds — it's the volatility that makes Black-Scholes reproduce the option's own
  traded price. We already fetch that price (the per-contract daily bar close, below) plus
  the underlying's close and a risk-free rate, so `option_greeks.py` inverts Black-Scholes
  per bar to get real point-in-time iv/delta/gamma/theta/vega — no vendor needed. See
  `docs/plans/2026-07-08-options-backtest-data-gap-analysis.md` for the full rationale.
  Caveat: pure Black-Scholes (European); equity options are American, so deep-ITM puts near
  ex-dividend have a small known bias. Open interest/volume are still always None (Alpaca has
  no as-of OI/volume for a past date; no way to derive those from price alone).

WHAT this builds:
  1. CONTRACT DISCOVERY incl. EXPIRED: `get_option_contracts` defaults to status=ACTIVE and
     MISSES expired contracts, so for a historical window we query BOTH status=INACTIVE and
     status=ACTIVE and merge by OCC symbol (dedup). Expiries are bounded to the run window.
  2. CHAIN ROWS from CONTRACT METADATA (occ/type/strike/expiry) + computed iv/greeks for the
     run `start` date (a single as-of snapshot — the PER-DAY greeks live on the bars, below;
     `HistoricalOptionsProvider.get_chain` overlays the as-of bar's greeks onto this row).
  3. PER-CONTRACT DAILY BARS via `get_option_bars` (this DOES work for historical dates) →
     the premium series the fill engine reads, each carrying its OWN computed iv/greeks.
  4. PRACTICALITY NARROWING: --strike-min/--strike-max and --max-contracts so a build stays
     bounded (a wide window can otherwise be thousands of contracts).
"""
from __future__ import annotations
import argparse
import logging
import re
import time as _time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from ba2_common.core.types import OptionRight
from .option_greeks import compute_iv_and_greeks
from .options_cache import OptionsHistoryCache

logger = logging.getLogger(__name__)

# Alpaca options-history floor: no chain/bar data exists before this date.
_OPTIONS_HISTORY_FLOOR = date(2024, 2, 1)
# How far past `end` to still pull contracts (so a position opened near `end` can pick an
# expiry that lands after the window). Matches the handler's DTE windows comfortably.
_EXPIRY_TAIL_DAYS = 60
# Contracts per get_option_bars request. Alpaca accepts a list of symbols per call (paginating
# internally), so batching collapses thousands of per-contract round-trips into dozens. Kept
# modest so the request URL and per-response payload stay well within limits.
_OPTION_BARS_BATCH = 100
# Standard OCC option-symbol pattern Alpaca's market-data API validates against. Corporate-
# action ADJUSTED contracts come back with a non-standard root (e.g. "1AAPL240429P00170000")
# that the bars endpoint REJECTS (400 invalid symbol). They are not normally tradeable on the
# %OTM/DTE path, so we drop them at discovery rather than crash the whole build on one symbol.
_OCC_RE = re.compile(r"^[A-Z]{1,5}\d{6,7}[CP]\d{8}$")


def is_standard_occ(symbol: str) -> bool:
    """True iff ``symbol`` is a standard (non-adjusted) OCC option symbol the bars endpoint
    accepts. Filters corporate-action adjusted contracts (numeric/extra-char roots) that
    would otherwise 400 the per-contract bar fetch."""
    return bool(_OCC_RE.match(symbol or ""))


def _g(obj, name):
    return getattr(obj, name, None)


def contract_to_chain_row(occ: str, underlying: str, opt_type: str, strike: float,
                          expiry: str, snap: Any = None) -> Dict[str, Any]:
    """Map a contract (+ optional CURRENT snapshot) to a chain row.

    For the HISTORICAL cache `snap` is None (or a metadata-only object), so greeks / iv /
    open_interest / volume come out as None — Alpaca has no as-of greeks for a past date, and
    selection is by %OTM/DTE not delta. The snapshot path is kept (back-compat) but is only
    meaningful for a CURRENT build; a historical build calls this with snap=None."""
    greeks = _g(snap, "greeks"); q = _g(snap, "latest_quote"); t = _g(snap, "latest_trade")
    return {"occ_symbol": occ, "option_type": opt_type, "strike": strike, "expiry": expiry,
            "bid": _g(q, "bid_price"), "ask": _g(q, "ask_price"), "last": _g(t, "price"),
            "iv": _g(snap, "implied_volatility"), "delta": _g(greeks, "delta"),
            "gamma": _g(greeks, "gamma"), "theta": _g(greeks, "theta"), "vega": _g(greeks, "vega"),
            "open_interest": _g(snap, "open_interest"), "volume": _g(t, "size")}


def contract_to_metadata_chain_row(c: Any, underlying: str,
                                   as_of_premium: Optional[float] = None, *,
                                   as_of_date: Optional[date] = None,
                                   underlying_close: Optional[float] = None,
                                   risk_free_rate: float = 0.0) -> Dict[str, Any]:
    """Map an Alpaca OptionContract (metadata) to a HISTORICAL chain row.

    Pure (no network). open_interest/volume are ALWAYS None — Alpaca has no as-of OI/volume for
    a past date. iv/delta/gamma/theta/vega are computed OURSELVES via Black-Scholes inversion
    (see ``option_greeks.py``) when BOTH ``as_of_date`` and ``underlying_close`` are supplied —
    IV is derived from the option's own price, not an independently-observed vendor quantity, so
    we don't need Alpaca (or anyone) to hand us historical greeks. Omit either kwarg (as the old
    call sites and most tests do) to get the prior None-filled behaviour unchanged.

    ``as_of_premium`` (the contract's CLOSE on the chain's as-of date, taken from the daily bar
    we already fetch) is used to fill bid/ask/last so the option ENTRY action — which requires a
    non-None ``ask`` to size + price the order (see TradeActions ``_build_and_submit``) — has a
    real historical premium to work from. We have no as-of bid/ask SPREAD, so we set
    bid=ask=last=close (a zero-spread historical-premium proxy); the actual FILL still comes from
    the per-bar premium series via ``_option_fill_price``. When no as-of bar exists (the contract
    did not trade that day) they stay None and that contract is simply not selectable that day.

    ``c.type``/``c.expiration_date`` may be enums/dates or already-normalised strings (so the same
    mapper serves real contracts AND test stubs)."""
    opt_type = c.type.value if hasattr(c.type, "value") else str(c.type)
    exp = c.expiration_date
    expiry = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)
    px = float(as_of_premium) if as_of_premium is not None else None
    greeks_out = {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
    if px is not None and as_of_date is not None and underlying_close is not None:
        expiry_d = exp if isinstance(exp, date) else date.fromisoformat(str(expiry))
        T = (expiry_d - as_of_date).days / 365.0
        greeks_out.update(compute_iv_and_greeks(
            px, underlying_close, float(c.strike_price), T, risk_free_rate,
            OptionRight(opt_type)))
    return {"occ_symbol": c.symbol, "option_type": opt_type, "strike": float(c.strike_price),
            "expiry": expiry, "bid": px, "ask": px, "last": px,
            **greeks_out, "open_interest": None, "volume": None}


def merge_contracts_by_symbol(*contract_lists: List[Any]) -> List[Any]:
    """Merge several contract lists (e.g. INACTIVE + ACTIVE) deduping on OCC symbol.

    First occurrence wins; pass the INACTIVE (expired) list FIRST so the historical contracts
    are kept (they carry the same immutable strike/expiry/type metadata either way). Pure — no
    network — so it is unit-tested directly."""
    seen: Dict[str, Any] = {}
    out: List[Any] = []
    for lst in contract_lists:
        for c in lst or []:
            sym = c.symbol
            if sym in seen:
                continue
            seen[sym] = c
            out.append(c)
    return out


def bar_to_row(occ: str, d: str, bar: Any, underlying: str, opt_type: str, strike: float,
               expiry: str, *, underlying_close: Optional[float] = None,
               risk_free_rate: float = 0.0) -> Dict[str, Any]:
    """Map one daily option bar to a row. iv/delta/gamma/theta/vega are computed via
    Black-Scholes inversion of THIS bar's close (see ``option_greeks.py``) when
    ``underlying_close`` is supplied — the POINT-IN-TIME greeks for this specific trading day,
    not a single build-time snapshot. Omit it (as the pre-existing tests do) for the prior
    None-filled behaviour."""
    close = _g(bar, "close")
    greeks_out = {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
    if close is not None and underlying_close is not None:
        T = (date.fromisoformat(expiry) - date.fromisoformat(d)).days / 365.0
        greeks_out.update(compute_iv_and_greeks(
            float(close), underlying_close, strike, T, risk_free_rate, OptionRight(opt_type)))
    return {"occ_symbol": occ, "date": d, "open": _g(bar, "open"), "high": _g(bar, "high"),
            "low": _g(bar, "low"), "close": close, "volume": _g(bar, "volume"),
            "underlying": underlying, "option_type": opt_type, "strike": strike, "expiry": expiry,
            **greeks_out}


def _alpaca_keys(key: Optional[str] = None, secret: Optional[str] = None) -> Tuple[str, str]:
    """Resolve Alpaca creds. Explicit key/secret args win (so a caller/test can inject the
    live-DB key WITHOUT touching the environment); otherwise read from the env.

    The codebase configures Alpaca market-data creds as ALPACA_MARKET_API_KEY/_SECRET
    (see .env / .env.example); fall back to the generic ALPACA_API_KEY/_SECRET_KEY names."""
    if key and secret:
        return key, secret
    import os
    key = key or os.environ.get("ALPACA_MARKET_API_KEY") or os.environ.get("ALPACA_API_KEY")
    secret = secret or os.environ.get("ALPACA_MARKET_API_SECRET") or os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(
            "Alpaca credentials not found. Pass key/secret to build_cache, or set "
            "ALPACA_MARKET_API_KEY/ALPACA_MARKET_API_SECRET (or ALPACA_API_KEY/"
            "ALPACA_SECRET_KEY) in the environment / .env.")
    return key, secret


def _options_feed(feed: str) -> Any:
    """Resolve the CLI/build_cache ``feed`` string to alpaca's ``OptionsFeed`` enum, matching its
    values case-insensitively ("indicative" / "opra"). Fails LOUD on anything else — no silent
    fallback to the default feed: which feed the bars come from is a data-correctness choice
    (the indicative feed's quote-derived prints can be arbitrage-inconsistent with the
    underlying), so a typo must error, not silently fetch the wrong feed."""
    from alpaca.data.enums import OptionsFeed
    by_value = {f.value: f for f in OptionsFeed}
    try:
        return by_value[str(feed).strip().lower()]
    except KeyError:
        raise ValueError(
            f"invalid options feed {feed!r}; valid values: {sorted(by_value)}") from None


# Default risk-free rate used when FRED is unreachable/unconfigured. Rho (rate sensitivity) is
# the smallest-impact Greek for short-dated equity options, so a rough constant is a far smaller
# error source than the close-price-based IV itself — this is a documented fallback, not a
# silently-wrong default (a warning is logged when it's used).
_FALLBACK_RISK_FREE_RATE = 0.045


def fetch_risk_free_rate_series(start: date, end: date) -> Dict[str, float]:
    """Daily risk-free rate (3-month Treasury, FRED series DGS3MO) as {date_iso: rate_decimal}
    over [start, end], forward-filled across weekends/holidays (FRED only publishes business
    days). One HTTP call for the whole build (shared across every underlying/contract) — this is
    the SAME external input `FREDMacroProvider` already exposes elsewhere in the codebase, called
    directly here (env var key, not the DB-backed AppSetting) so this script stays a
    self-contained CLI like its Alpaca creds handling.

    Falls back to `_FALLBACK_RISK_FREE_RATE` (with a logged warning) when FRED_API_KEY is unset
    or the request fails — rho is a minor Greek, so this does not block the build."""
    import os
    import requests
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        logger.warning(
            "FRED_API_KEY not set; using a flat %.2f%% risk-free rate for option greeks "
            "(rho is a minor Greek, this does not materially affect delta/gamma/theta/vega).",
            _FALLBACK_RISK_FREE_RATE * 100)
        return {}
    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "DGS3MO", "api_key": api_key, "file_type": "json",
                    "observation_start": start.isoformat(), "observation_end": end.isoformat(),
                    "sort_order": "asc", "limit": 10000},
            timeout=30)
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
    except Exception as e:  # noqa: BLE001 — never block the build on a macro-data hiccup
        logger.warning(f"FRED risk-free-rate fetch failed ({e}); using flat "
                       f"{_FALLBACK_RISK_FREE_RATE * 100:.2f}% fallback.")
        return {}
    series: Dict[str, float] = {}
    last: Optional[float] = None
    for o in obs:
        v = o.get("value")
        if v and v != ".":
            last = float(v) / 100.0
        if last is not None:
            series[o["date"]] = last
    return series


def fetch_underlying_close_series(ohlcv_provider: Any, underlying: str,
                                  start: date, end: date) -> Dict[str, float]:
    """Daily underlying close as {date_iso: close} over [start, end] via the SAME cached
    ba2_providers OHLCV path the backtest engine reads (`get_ohlcv_data` — parquet-cached, so a
    warm cache costs no network call). Returns {} on any read failure (missing symbol, provider
    error) rather than aborting the whole underlying's options build."""
    from datetime import datetime as _dt
    try:
        df = ohlcv_provider.get_ohlcv_data(
            underlying, start_date=_dt.combine(start, _dt.min.time()),
            end_date=_dt.combine(end, _dt.min.time()), interval="1d")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"underlying close fetch failed for {underlying} ({e}); "
                       f"greeks will be skipped for this underlying.")
        return {}
    if df is None or df.empty:
        return {}
    return {row["Date"].strftime("%Y-%m-%d") if hasattr(row["Date"], "strftime") else str(row["Date"])[:10]:
            float(row["Close"]) for _, row in df.iterrows()}


def _nearest_on_or_before(series: Dict[str, float], d: date, max_back_days: int = 7) -> Optional[float]:
    """Value at ``d`` if present, else the nearest PRIOR date within ``max_back_days`` (as-of
    clamp for a sparse daily series — weekends/holidays have no Treasury/equity print)."""
    for i in range(max_back_days + 1):
        key = (d - timedelta(days=i)).isoformat()
        if key in series:
            return series[key]
    return None


def discover_contracts(tc: Any, underlying: str, *, expiry_gte: str, expiry_lte: str,
                       strike_min: Optional[float] = None, strike_max: Optional[float] = None,
                       max_contracts: Optional[int] = None) -> List[Any]:
    """Discover contracts for ``underlying`` over the window INCLUDING EXPIRED ones.

    Queries BOTH status=INACTIVE (the expired/historical contracts the default ACTIVE query
    misses) AND status=ACTIVE, then merges by OCC symbol (INACTIVE first → historical kept).
    Expiries are bounded by ``expiry_gte``/``expiry_lte``; optional ``strike_min``/``strike_max``
    narrow the strike band and ``max_contracts`` caps the build size."""
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import AssetStatus

    def _fetch(status: AssetStatus) -> List[Any]:
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying], status=status,
            expiration_date_gte=expiry_gte, expiration_date_lte=expiry_lte,
            strike_price_gte=str(strike_min) if strike_min is not None else None,
            strike_price_lte=str(strike_max) if strike_max is not None else None,
            limit=10000)
        return tc.get_option_contracts(req).option_contracts or []

    inactive = _fetch(AssetStatus.INACTIVE)   # expired/historical — the important leg
    active = _fetch(AssetStatus.ACTIVE)        # still-listed (window may overlap "now")
    merged = merge_contracts_by_symbol(inactive, active)
    # Drop corporate-action ADJUSTED contracts (non-standard OCC root) — the bars endpoint
    # rejects them and they are not on the normal %OTM/DTE selection path.
    merged = [c for c in merged if is_standard_occ(c.symbol)]
    if max_contracts is not None and len(merged) > max_contracts:
        # Deterministic cap: keep strikes nearest the band centre so a narrowed build keeps
        # the most useful (near-the-money) strikes rather than an arbitrary slice.
        if strike_min is not None and strike_max is not None:
            center = (strike_min + strike_max) / 2.0
            merged = sorted(merged, key=lambda c: abs(float(c.strike_price) - center))
        merged = merged[:max_contracts]
    return merged


def _is_transient(e: Exception) -> bool:
    """A retryable network/server condition (Alpaca's option endpoint drops connections under
    load) vs a permanent error (bad symbol/auth) that should fail fast."""
    s = repr(e)
    return any(m in s for m in (
        "RemoteDisconnected", "Connection aborted", "ConnectionError", "ConnectionResetError",
        "timed out", "Timeout", "Max retries", "TooManyRequests", "429",
        "502", "503", "504", "Temporarily"))


def _with_retry(fn, *, what: str, delays=(5, 15, 30, 60)):
    """Call ``fn()`` with exponential backoff on transient Alpaca/connection errors (mirrors the
    FMP ``fmp_http_get`` retry contract). Non-transient errors raise immediately; transient ones
    raise only after the delays are exhausted."""
    last: Optional[Exception] = None
    for attempt in range(len(delays) + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_transient(e) or attempt == len(delays):
                raise
            d = delays[attempt]
            logger.warning(f"options fetch {what}: transient error "
                           f"(attempt {attempt + 1}/{len(delays) + 1}): {e}; retry in {d}s")
            _time.sleep(d)
    raise last  # unreachable


def build_cache(cache_db: str, underlyings: List[str], start: date, end: date,
                feed: str = "indicative", *,
                strike_min: Optional[float] = None, strike_max: Optional[float] = None,
                max_contracts: Optional[int] = None,
                api_key: Optional[str] = None, api_secret: Optional[str] = None,
                max_workers: Optional[int] = None, resume: bool = True,
                paper: bool = True) -> Dict[str, int]:
    """Build a HISTORICAL options cache (expired contracts + metadata chain + daily bars).

    Resilient like the FMP fetcher: underlyings are fetched CONCURRENTLY (ThreadPoolExecutor,
    ``max_workers`` or $OPTIONS_FETCH_WORKERS, default 6) and every Alpaca call backs off + retries
    on transient drops, so one ``RemoteDisconnected`` can't abort a 2000-symbol run. ``resume=True``
    skips underlyings already fully cached (present in option_chain, written LAST per symbol). A
    symbol that still fails after retries is logged + skipped (counted in ``failed``), not fatal.

    Returns ``{"chain_rows","bar_rows","contracts","symbols_done","symbols_failed","skipped"}``.
    iv/delta/gamma/theta/vega are computed via Black-Scholes inversion of each bar's own close
    (see ``option_greeks.py``) — real point-in-time greeks, not vendor data. Pass
    ``api_key``/``api_secret`` to inject creds.

    ``paper`` (default True) selects which Alpaca environment the TradingClient (contract
    discovery) authenticates against. A LIVE-only key has no paper-account counterpart and is
    REJECTED (40110000 "request is not authorized") if paper=True — pass paper=False when
    ``api_key``/``api_secret`` are a live account's keys. Contract discovery is read-only
    (GetOptionContractsRequest), so paper=False here places no live orders.

    ``feed`` selects the Alpaca options data feed the daily BARS are fetched from
    ("indicative", the free default, or "opra", trades — requires the OPRA subscription). It
    is honored on every option-data request below; an invalid value raises ValueError before
    any network call."""
    import os as _os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.enums import OptionsFeed
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from ba2_providers import get_provider
    if start < _OPTIONS_HISTORY_FLOOR:
        raise ValueError(
            f"Alpaca options history starts {_OPTIONS_HISTORY_FLOOR.isoformat()}; pick a later --start")
    options_feed = _options_feed(feed)   # fail loud on a bad feed BEFORE any network/cred work
    key, secret = _alpaca_keys(api_key, api_secret)
    cache = OptionsHistoryCache(cache_db)

    # alpaca-py (0.43.4) gives OptionBarsRequest NO feed field (only the snapshot/chain/latest
    # option request classes carry one) and its pydantic models silently IGNORE unknown kwargs,
    # so feed= on the base class would be a silent no-op — the exact dead-flag bug this guards
    # against. Declaring the field via a subclass makes to_request_fields() emit `feed`, which
    # the /options/bars endpoint honors (same serialization path as the SDK's own feed-bearing
    # request classes).
    class _FedOptionBarsRequest(OptionBarsRequest):
        feed: Optional[OptionsFeed] = None

    expiry_gte = start.isoformat()
    expiry_lte = (end + timedelta(days=_EXPIRY_TAIL_DAYS)).isoformat()
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    # Risk-free rate: ONE FRED call for the whole build (read-only dict, safe to share across
    # threads). Underlying close series is fetched PER-UNDERLYING inside _process (below) since
    # it needs a per-thread OHLCV provider instance, mirroring the per-thread Alpaca clients.
    risk_free_series = fetch_risk_free_rate_series(start, end)

    pending = list(underlyings)
    skipped = 0
    if resume:
        done = cache.cached_underlyings()
        pending = [u for u in underlyings if u not in done]
        skipped = len(underlyings) - len(pending)
    logger.info(f"options fetch: {len(underlyings)} requested, {skipped} already cached, "
                f"{len(pending)} to fetch")

    stats = {"chain_rows": 0, "bar_rows": 0, "contracts": 0,
             "symbols_done": 0, "symbols_failed": 0, "skipped": skipped}
    write_lock = threading.Lock()
    stats_lock = threading.Lock()
    # Per-thread Alpaca clients (requests.Session under the hood isn't guaranteed thread-safe).
    _tl = threading.local()

    def _clients():
        if not hasattr(_tl, "tc"):
            _tl.tc = TradingClient(key, secret, paper=paper)
            _tl.dc = OptionHistoricalDataClient(key, secret)
            _tl.ohlcv = get_provider("ohlcv", "fmp")
        return _tl.tc, _tl.dc, _tl.ohlcv

    def _rate_at(d: date) -> float:
        r = _nearest_on_or_before(risk_free_series, d)
        return r if r is not None else _FALLBACK_RISK_FREE_RATE

    def _process(u: str) -> None:
        try:
            tc, dc, ohlcv = _clients()
            contracts = _with_retry(
                lambda: discover_contracts(tc, u, expiry_gte=expiry_gte, expiry_lte=expiry_lte,
                                           strike_min=strike_min, strike_max=strike_max,
                                           max_contracts=max_contracts),
                what=f"discover {u}")
            # Daily bars: Alpaca's get_option_bars accepts a LIST of symbols per request (and
            # paginates internally), so fetch in BATCHES instead of one HTTP round-trip per
            # contract. A wide window yields thousands of contracts/underlying; per-contract calls
            # made this ~100x slower than necessary. _OPTION_BARS_BATCH symbols/call keeps the
            # request URL/response bounded while collapsing thousands of round-trips into dozens.
            syms = [c.symbol for c in contracts]
            bars_by_sym: Dict[str, List[Any]] = {}
            for i in range(0, len(syms), _OPTION_BARS_BATCH):
                chunk = syms[i:i + _OPTION_BARS_BATCH]
                resp = _with_retry(
                    lambda chunk=chunk: dc.get_option_bars(_FedOptionBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                        start=start_iso, end=end_iso, feed=options_feed)),
                    what=f"bars {u} [{i // _OPTION_BARS_BATCH + 1}]")
                for s, blist in (resp.data or {}).items():
                    bars_by_sym[s] = blist
            # Underlying daily close, ONE fetch per underlying (cached parquet — the same path the
            # backtest engine reads), reused for every contract/bar so we can Black-Scholes-invert
            # each bar's OWN close into point-in-time iv/delta/gamma/theta/vega (see
            # option_greeks.py) — no vendor greeks needed, we derive them from data we already pull.
            underlying_close = fetch_underlying_close_series(ohlcv, u, start, end)
            rows: List[Dict[str, Any]] = []
            all_bar_rows: List[Dict[str, Any]] = []
            for c in contracts:
                opt_type = c.type.value if hasattr(c.type, "value") else str(c.type)
                expiry = (c.expiration_date.isoformat()
                          if hasattr(c.expiration_date, "isoformat") else str(c.expiration_date))
                bars = bars_by_sym.get(c.symbol, [])
                bar_rows = []
                for b in bars:
                    bar_date = b.timestamp.date()
                    S = _nearest_on_or_before(underlying_close, bar_date)
                    bar_rows.append(bar_to_row(
                        c.symbol, bar_date.isoformat(), b, u, opt_type, float(c.strike_price),
                        expiry, underlying_close=S, risk_free_rate=_rate_at(bar_date)))
                all_bar_rows.extend(bar_rows)
                as_of_premium: Optional[float] = None
                if bar_rows:
                    on_start = next((r for r in bar_rows if r["date"] == start_iso), None)
                    as_of_premium = (on_start or bar_rows[0]).get("close")
                rows.append(contract_to_metadata_chain_row(
                    c, u, as_of_premium, as_of_date=start,
                    underlying_close=_nearest_on_or_before(underlying_close, start),
                    risk_free_rate=_rate_at(start)))
            bar_total = len(all_bar_rows)
            # One batched write of all bars, then chain rows LAST (their presence marks this
            # underlying complete for resume).
            with write_lock:
                if all_bar_rows:
                    cache.write_bar_rows(all_bar_rows)
                cache.write_chain_rows(u, start_iso, rows)
            with stats_lock:
                stats["contracts"] += len(contracts)
                stats["bar_rows"] += bar_total
                stats["chain_rows"] += len(rows)
                stats["symbols_done"] += 1
                if stats["symbols_done"] % 25 == 0:
                    logger.info(f"options fetch: {stats['symbols_done']}/{len(pending)} done "
                                f"({stats['symbols_failed']} failed)")
        except Exception as e:  # noqa: BLE001 — give up on THIS symbol, keep the run going
            logger.error(f"options fetch: giving up on {u} after retries: {e}")
            with stats_lock:
                stats["symbols_failed"] += 1

    workers = max_workers or int(_os.environ.get("OPTIONS_FETCH_WORKERS", "6"))
    workers = max(1, min(workers, len(pending) or 1))
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_process, pending))
    logger.info(f"options fetch DONE: {stats}")
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ba2-test fetch-options")
    ap.add_argument("--underlyings", required=True, help="comma list or @file")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--cache-db", required=True)
    ap.add_argument("--feed", default="indicative",
                    help="options data feed the daily bars are fetched from — 'indicative' "
                         "(default, free; quote-derived prints) or 'opra' (trades; requires "
                         "the OPRA subscription). Honored on the bars request; invalid values "
                         "are rejected.")
    ap.add_argument("--strike-min", type=float, default=None, help="narrow strikes >= this")
    ap.add_argument("--strike-max", type=float, default=None, help="narrow strikes <= this")
    ap.add_argument("--max-contracts", type=int, default=None,
                    help="cap contracts fetched (nearest the strike-band centre)")
    ap.add_argument("--live", action="store_true",
                    help="Authenticate contract discovery against the LIVE Alpaca environment "
                         "instead of paper (required for a live-only key; read-only, places no "
                         "orders).")
    a = ap.parse_args(argv)
    unders = (open(a.underlyings[1:]).read().split() if a.underlyings.startswith("@")
              else [s.strip() for s in a.underlyings.split(",") if s.strip()])
    stats = build_cache(a.cache_db, unders, date.fromisoformat(a.start), date.fromisoformat(a.end),
                        a.feed, strike_min=a.strike_min, strike_max=a.strike_max,
                        max_contracts=a.max_contracts, paper=not a.live)
    print(f"built options cache: {stats}")
    return 0
