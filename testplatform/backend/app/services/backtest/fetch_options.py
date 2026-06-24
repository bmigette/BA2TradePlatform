"""Builds the offline HISTORICAL options cache from Alpaca. CLI: `ba2-test fetch-options`.
Run with the editable venv (~/ba2-venvs/test) which has alpaca-py installed.

WHY a metadata-driven historical cache (NOT the chain snapshot):
  Alpaca's option CHAIN endpoint (`get_option_chain`) is a CURRENT snapshot only — it has
  no as-of greeks / IV / OI for a past date, and it does NOT return EXPIRED contracts. A
  trustworthy historical cache therefore CANNOT carry as-of greeks/IV, so option selection
  at backtest time is by **%OTM (strike vs spot) + DTE**, NOT by delta. (`option_selector`'s
  delta method would return nothing here because `delta` is None — documented limitation.)

WHAT this builds:
  1. CONTRACT DISCOVERY incl. EXPIRED: `get_option_contracts` defaults to status=ACTIVE and
     MISSES expired contracts, so for a historical window we query BOTH status=INACTIVE and
     status=ACTIVE and merge by OCC symbol (dedup). Expiries are bounded to the run window.
  2. CHAIN ROWS from CONTRACT METADATA (occ/type/strike/expiry), greeks/iv/oi/volume = None,
     keyed at the run `start` date (a single as-of snapshot; acceptable for a short window).
  3. PER-CONTRACT DAILY BARS via `get_option_bars` (this DOES work for historical dates) →
     the premium series the fill engine reads.
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
                                   as_of_premium: Optional[float] = None) -> Dict[str, Any]:
    """Map an Alpaca OptionContract (metadata) to a HISTORICAL chain row.

    Pure (no network). greeks/iv/open_interest/volume are ALWAYS None — a historical cache has
    no as-of greeks/IV; selection at backtest time is %OTM (strike vs spot) + DTE, which needs
    only occ_symbol/option_type/strike/expiry.

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
    return {"occ_symbol": c.symbol, "option_type": opt_type, "strike": float(c.strike_price),
            "expiry": expiry, "bid": px, "ask": px, "last": px,
            "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None,
            "open_interest": None, "volume": None}


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
               expiry: str) -> Dict[str, Any]:
    return {"occ_symbol": occ, "date": d, "open": _g(bar, "open"), "high": _g(bar, "high"),
            "low": _g(bar, "low"), "close": _g(bar, "close"), "volume": _g(bar, "volume"),
            "underlying": underlying, "option_type": opt_type, "strike": strike, "expiry": expiry}


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
                max_workers: Optional[int] = None, resume: bool = True) -> Dict[str, int]:
    """Build a HISTORICAL options cache (expired contracts + metadata chain + daily bars).

    Resilient like the FMP fetcher: underlyings are fetched CONCURRENTLY (ThreadPoolExecutor,
    ``max_workers`` or $OPTIONS_FETCH_WORKERS, default 6) and every Alpaca call backs off + retries
    on transient drops, so one ``RemoteDisconnected`` can't abort a 2000-symbol run. ``resume=True``
    skips underlyings already fully cached (present in option_chain, written LAST per symbol). A
    symbol that still fails after retries is logged + skipped (counted in ``failed``), not fatal.

    Returns ``{"chain_rows","bar_rows","contracts","symbols_done","symbols_failed","skipped"}``.
    Selection downstream is %OTM/DTE (no greeks). Pass ``api_key``/``api_secret`` to inject creds."""
    import os as _os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionBarsRequest
    from alpaca.data.timeframe import TimeFrame
    if start < _OPTIONS_HISTORY_FLOOR:
        raise ValueError(
            f"Alpaca options history starts {_OPTIONS_HISTORY_FLOOR.isoformat()}; pick a later --start")
    key, secret = _alpaca_keys(api_key, api_secret)
    cache = OptionsHistoryCache(cache_db)

    expiry_gte = start.isoformat()
    expiry_lte = (end + timedelta(days=_EXPIRY_TAIL_DAYS)).isoformat()
    start_iso = start.isoformat()
    end_iso = end.isoformat()

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
            _tl.tc = TradingClient(key, secret, paper=True)
            _tl.dc = OptionHistoricalDataClient(key, secret)
        return _tl.tc, _tl.dc

    def _process(u: str) -> None:
        try:
            tc, dc = _clients()
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
                    lambda chunk=chunk: dc.get_option_bars(OptionBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                        start=start_iso, end=end_iso)),
                    what=f"bars {u} [{i // _OPTION_BARS_BATCH + 1}]")
                for s, blist in (resp.data or {}).items():
                    bars_by_sym[s] = blist
            rows: List[Dict[str, Any]] = []
            all_bar_rows: List[Dict[str, Any]] = []
            for c in contracts:
                opt_type = c.type.value if hasattr(c.type, "value") else str(c.type)
                expiry = (c.expiration_date.isoformat()
                          if hasattr(c.expiration_date, "isoformat") else str(c.expiration_date))
                bars = bars_by_sym.get(c.symbol, [])
                bar_rows = [bar_to_row(c.symbol, b.timestamp.date().isoformat(), b, u,
                                       opt_type, float(c.strike_price), expiry) for b in bars]
                all_bar_rows.extend(bar_rows)
                as_of_premium: Optional[float] = None
                if bar_rows:
                    on_start = next((r for r in bar_rows if r["date"] == start_iso), None)
                    as_of_premium = (on_start or bar_rows[0]).get("close")
                rows.append(contract_to_metadata_chain_row(c, u, as_of_premium))
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
    ap.add_argument("--cache-db", required=True); ap.add_argument("--feed", default="indicative")
    ap.add_argument("--strike-min", type=float, default=None, help="narrow strikes >= this")
    ap.add_argument("--strike-max", type=float, default=None, help="narrow strikes <= this")
    ap.add_argument("--max-contracts", type=int, default=None,
                    help="cap contracts fetched (nearest the strike-band centre)")
    a = ap.parse_args(argv)
    unders = (open(a.underlyings[1:]).read().split() if a.underlyings.startswith("@")
              else [s.strip() for s in a.underlyings.split(",") if s.strip()])
    stats = build_cache(a.cache_db, unders, date.fromisoformat(a.start), date.fromisoformat(a.end),
                        a.feed, strike_min=a.strike_min, strike_max=a.strike_max,
                        max_contracts=a.max_contracts)
    print(f"built options cache: {stats}")
    return 0
