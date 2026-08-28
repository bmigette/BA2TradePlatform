"""As-of-clamped reader over the TastyTrade/dxfeed PARQUET option store.

THE SECOND BACKEND, NOT A SECOND ENGINE. ``HistoricalOptionsProvider`` (options_provider.py)
reads the Alpaca-built ``OptionsHistoryCache`` sqlite. This class reads
``CACHE_FOLDER/TastyTradeOptionsProvider/<SYM>/exp=<YYYY-MM-DD>/<SYM>_<exp>_1d.parquet``
(written by ``tools/warm_options_history.py`` via
``ba2_providers.options.parquet_store.OptionHistoryParquetStore``) and exposes the SAME four
methods with the SAME signatures and the SAME as-of clamp, so ``BacktestAccount`` cannot tell
them apart. The engine's contract is exactly:

    get_chain(underlying, as_of, *, expiry_min, expiry_max,
              option_type=None, strike_min=None, strike_max=None) -> List[OptionContract]
    get_quote(occ_symbol, as_of)  -> Optional[OptionQuote]
    get_bar(occ_symbol, as_of)    -> Optional[dict]   # EXACT bar on as_of, else None
    get_atm_iv(underlying, as_of) -> Optional[float]

(enumerated from ``backtest_account.py``: get_option_chain / get_option_quote /
get_atm_implied_volatility / ``_options.get_bar`` at the MTM, liquidation, fill,
round-trip-recorder and expiry-settlement sites).

WHICH STORE A RUN READS IS AN EXPLICIT CHOICE THAT DEFAULTS TO SQLITE — see
``options_store.py``. Every backtest number on record was produced against the sqlite path
and nothing here may perturb it.

WHAT THE PARQUET HAS THAT THE SQLITE DOES NOT
  * ``open_interest`` — POPULATED (26,853 of 27,974 GOOG rows). The sqlite's is NULL on every
    one of its 6,757,055 chain rows, so ``option_selector``'s ``min_open_interest`` gate is
    un-answerable there and becomes usable here.
  * ``iv`` — the VENDOR's implied volatility, populated (26,840 of 27,974 GOOG rows). It is
    carried through on the bar dict as ``vendor_iv`` but is NOT what selection reads; see
    GREEKS below.

WHAT IT DOES NOT HAVE, AND WHY THAT IS NOT A REGRESSION
  * greeks (delta/gamma/theta/vega) are ABSENT. They are derived exactly the way the sqlite
    store's own bars were derived at BUILD time: one call to
    ``option_greeks.compute_iv_and_greeks`` per (contract, bar), Black-Scholes-inverting THAT
    bar's own close against the underlying's close on THAT date. Same function, same model,
    same convention (theta per calendar day, vega per vol point) — there is deliberately no
    second greeks path in this file.
    Consequence worth stating: ``implied_volatility`` reported here is the INVERTED iv, not
    the vendor's, so that it and ``delta`` are the same number's consequences. Measured on
    GOOG's 25,864 comparable rows the two differ by a median 0.034 / mean 0.060 / p90 0.117
    of a vol unit — close in kind, not equal. ``vendor_iv`` is preserved on the bar dict so a
    later change can prefer it without re-reading 205 MB.
  * bid/ask are ABSENT (dxfeed serves no historical NBBO for dead contracts) and no worse
    than sqlite, where ``bid == ask`` on all 6,757,055 rows: both stores are a ZERO-SPREAD
    premium proxy and the tradeable spread is MODELLED downstream by ``option_spread_pct``.
    So bid = ask = last = the clamped bar's close, which is literally what
    ``fetch_options.contract_to_metadata_chain_row`` writes into the sqlite chain. Returning
    None instead would be "fail-loud" in name only: the option ENTRY action needs a non-None
    ``ask`` to size and price an order, so it would make the store unusable rather than
    honest.

SPOT IS INJECTED, NOT INVENTED. Black-Scholes needs the underlying price and NO option store
records one (the sqlite doesn't either — see ``options_provider.get_atm_iv``'s ATM PROXY
note). ``spot_source(underlying, bar_date) -> Optional[float]`` is supplied by the caller;
``options_store.build_options_provider`` wires it to the run's ``AsOfPriceSource``, whose
closes are the same FMP daily bars ``fetch_options`` inverted the sqlite store's greeks from.
It is only ever asked for the date of a bar that is ALREADY clamped to <= the engine clock,
so it cannot introduce lookahead. ``risk_free_rate`` is likewise a required constructor
argument — a pricing assumption, not data — with the single declared default living at the
wiring boundary.

AS-OF CLAMP (the whole point). The store is one row per contract per bar_date, so "the chain
on date D" is derived, not stored: the contract UNIVERSE on D is every contract with at least
one bar dated <= D, and each contract's row is its LATEST bar <= D. A contract whose first
bar is after D is not in the chain at all. That is the same shape as the sqlite reader
(``latest_as_of`` snapshot + per-contract ``latest_on_or_before`` overlay) with the snapshot
derived instead of stored, and like it, a stale-but-clamped bar is preferred to no row — the
fill engine still requires an EXACT bar on the fill day, so an untraded contract cannot fill.

CACHING — THE SAME SHAPE AS options_provider.py, FOR THE SAME REASON. A GA rebuilds the
provider once per trial from the same store and re-evaluates identical (symbol, date) pairs,
so reads are cached at WORKER-PROCESS level, not per instance:

  * ``_WORKER_UNDERLYING_CACHE`` — one ``_Underlying`` per (root, underlying, rate,
    spot_scope), holding the whole underlying's bars in columnar numpy plus a lazily-filled
    greeks overlay. This is the analogue of the sqlite reader's per-(db, underlying) chain
    cache AND its per-(db, contract) bar cache at once: the parquet unit of I/O is the
    underlying's partition set, so splitting them would buy nothing and cost a second LRU.
  * ``_WORKER_ATM_IV_CACHE`` — the get_atm_iv RESULT memo, mirroring the sqlite reader's.

Both are bounded LRUs (a remote worker's pool is long-lived across jobs touching different
universes) and both are dropped by ``clear_worker_parquet_options_cache()``, which
``options_provider.clear_worker_options_cache()`` also calls so existing test isolation covers
this store too.

``spot_scope`` is in both keys because the greeks are a function of the RUN's underlying
closes, unlike the sqlite store's, which were inverted once at build time and carry no
dependency on the reader. Without it, two runs in one pool worker over different windows
would share an overlay and the later one would silently invert bars against the earlier
one's forward-filled spot. See ``ParquetOptionsProvider.__init__``.

Columnar (numpy) rather than dict-per-bar because the cap has to clear a realistic universe:
686 underlyings x ~28k rows, and a run's ~100-symbol universe must fit in a worker alongside
the OHLCV memo. Measured: GOOG's 27,974 rows are 2.95 MB here. Bars are materialised into
dicts only when a caller actually reads one.
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from datetime import date, timedelta
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from ba2_providers.options.tastytrade import parse_occ

from .option_greeks import compute_iv_and_greeks
from .options_cache import OptionsCacheMiss

logger = logging.getLogger(__name__)

#: Underlyings held per worker process. Measured: GOOG's 27,974 rows cost 2.95 MB columnar
#: (13 data arrays + the 6 lazy greeks arrays + the contract-symbol list), so the default cap
#: is ~600 MB worst case and clears a 100-symbol option universe several times over. Sizing
#: it BELOW the run's universe is the thing to avoid — that thrashes (evict-then-reload)
#: inside a single bar, exactly as the sqlite reader's bar-cache comment warns. The penalty
#: is far gentler here than there (a reload is ~70 ms of parquet, not thousands of sqlite
#: round-trips), so lowering it on a memory-tight worker is a real option.
_UNDERLYING_CACHE_MAX = int(os.getenv("BT_OPTION_PARQUET_CACHE_MAX", "200"))
#: Same generosity (and same reasoning) as the sqlite reader's ATM-IV memo: the values are a
#: float or None, and a GA re-asks the identical (symbol, date) pairs on every trial.
_ATM_IV_CACHE_MAX = int(os.getenv("BT_OPTION_ATM_IV_CACHE_MAX", "200000"))

# Keys carry SPOT_SCOPE (see ParquetOptionsProvider.__init__): the cached greeks are a
# function of the underlying closes the run's price source serves, and two runs over
# different windows/universes do not serve the same ones.
_WORKER_UNDERLYING_CACHE: "OrderedDict[Tuple[str, str, float, str], _Underlying]" = OrderedDict()
_WORKER_ATM_IV_CACHE: "OrderedDict[Tuple[str, str, float, str, int], Optional[float]]" = OrderedDict()

#: Distinct from None, which is a VALID cached result. See options_provider._MISSING.
_MISSING = object()

#: ATM window, identical to the sqlite reader's so the two produce the same statistic.
_ATM_DTE_MIN = 20
_ATM_DTE_MAX = 45


def clear_worker_parquet_options_cache() -> None:
    """Drop every cached underlying + ATM-IV result (test isolation / explicit reset)."""
    _WORKER_UNDERLYING_CACHE.clear()
    _WORKER_ATM_IV_CACHE.clear()
    _underlying_of.cache_clear()


def _iso_to_ordinal_array(series) -> np.ndarray:
    """'YYYY-MM-DD' strings -> proleptic-Gregorian ordinals (int32).

    Ordinals, not YYYYMMDD: they are monotone in date (so ``searchsorted`` is the as-of clamp)
    AND their difference is a day count, which is exactly what the Black-Scholes ``T`` needs.
    """
    return np.array([date.fromisoformat(str(s)).toordinal() for s in series], dtype=np.int32)


class _Underlying:
    """One underlying's whole parquet history, columnar, with a lazy greeks overlay.

    Rows are sorted by (occ_symbol, bar_date); ``starts[i]:stops[i]`` is contract ``i``'s
    slice, so an as-of clamp is a ``searchsorted`` inside that slice.
    """

    __slots__ = (
        "underlying", "rate", "n_rows",
        "c_occ", "c_strike", "c_expiry_ord", "c_is_call", "c_index", "starts", "stops",
        "bar_ord", "open", "high", "low", "close", "volume", "open_interest", "vendor_iv",
        "_g_done", "_g_iv", "_g_delta", "_g_gamma", "_g_theta", "_g_vega",
        "_spot_source", "_spot_cache",
    )

    def __init__(self, underlying: str, rate: float, df,
                 spot_source: Callable[[str, date], Optional[float]]):
        self.underlying = underlying
        self.rate = float(rate)
        self._spot_source = spot_source
        self._spot_cache: Dict[int, Optional[float]] = {}

        if df is None or not len(df):
            self.n_rows = 0
            self.c_occ = []
            self.c_index = {}
            for name in ("c_strike", "c_expiry_ord", "c_is_call", "starts", "stops", "bar_ord",
                         "open", "high", "low", "close", "volume", "open_interest", "vendor_iv"):
                setattr(self, name, np.empty(0))
            self._g_done = np.empty(0, dtype=bool)
            for name in ("_g_iv", "_g_delta", "_g_gamma", "_g_theta", "_g_vega"):
                setattr(self, name, np.empty(0))
            return

        df = df.sort_values(["occ_symbol", "bar_date"], kind="mergesort").reset_index(drop=True)
        occ = df["occ_symbol"].astype(str).to_numpy(dtype=object)
        n = len(occ)
        self.n_rows = n

        is_new = np.empty(n, dtype=bool)
        is_new[0] = True
        if n > 1:
            is_new[1:] = occ[1:] != occ[:-1]
        starts = np.flatnonzero(is_new).astype(np.int32)
        self.starts = starts
        self.stops = np.append(starts[1:], np.int32(n)).astype(np.int32)

        self.c_occ = [str(s) for s in occ[starts]]
        self.c_index = {s: i for i, s in enumerate(self.c_occ)}
        self.c_strike = df["strike"].to_numpy(dtype="float64")[starts]
        self.c_expiry_ord = _iso_to_ordinal_array(df["expiry"].to_numpy(dtype=object)[starts])
        self.c_is_call = (df["option_type"].astype(str).to_numpy(dtype=object)[starts]
                          == OptionRight.CALL.value)

        self.bar_ord = _iso_to_ordinal_array(df["bar_date"].to_numpy(dtype=object))
        for col in ("open", "high", "low", "close"):
            setattr(self, col, df[col].to_numpy(dtype="float64", na_value=np.nan))
        self.vendor_iv = df["iv"].to_numpy(dtype="float64", na_value=np.nan)
        # volume/open_interest are pandas Int64 (nullable). float64 + nan keeps "absent"
        # distinguishable from a recorded 0, which is a fact about a strike nobody trades.
        self.volume = df["volume"].to_numpy(dtype="float64", na_value=np.nan)
        self.open_interest = df["open_interest"].to_numpy(dtype="float64", na_value=np.nan)

        self._g_done = np.zeros(n, dtype=bool)
        for name in ("_g_iv", "_g_delta", "_g_gamma", "_g_theta", "_g_vega"):
            setattr(self, name, np.full(n, np.nan, dtype="float64"))

    # -- as-of clamp ----------------------------------------------------
    def latest_row_on_or_before(self, ci: int, as_of_ord: int) -> int:
        """Row index of contract ``ci``'s latest bar dated <= ``as_of_ord``, or -1."""
        lo, hi = int(self.starts[ci]), int(self.stops[ci])
        j = int(np.searchsorted(self.bar_ord[lo:hi], as_of_ord, side="right"))
        return lo + j - 1 if j else -1

    def exact_row(self, ci: int, as_of_ord: int) -> int:
        """Row index of contract ``ci``'s bar dated EXACTLY ``as_of_ord``, or -1."""
        lo, hi = int(self.starts[ci]), int(self.stops[ci])
        j = int(np.searchsorted(self.bar_ord[lo:hi], as_of_ord, side="left"))
        if j >= hi - lo or int(self.bar_ord[lo + j]) != as_of_ord:
            return -1
        return lo + j

    # -- greeks ---------------------------------------------------------
    def _spot_on(self, bar_ord: int) -> Optional[float]:
        v = self._spot_cache.get(bar_ord, _MISSING)
        if v is _MISSING:
            v = self._spot_source(self.underlying, date.fromordinal(int(bar_ord)))
            self._spot_cache[bar_ord] = v
        return v

    def greeks_of_row(self, i: int, ci: int) -> Dict[str, Optional[float]]:
        """iv/delta/gamma/theta/vega for row ``i``, Black-Scholes-inverted from its own close.

        Memoised per row for the life of the cached underlying: a GA re-reads the same
        (contract, bar) pairs on every trial, and get_atm_iv alone re-scans a whole DTE band
        per bar. ``compute_iv_and_greeks`` is the ONE greeks path (11.2 us/call measured), the
        same one ``fetch_options.bar_to_row`` used to fill the sqlite store's bars.
        """
        if not self._g_done[i]:
            px = self.close[i]
            spot = self._spot_on(int(self.bar_ord[i]))
            t_days = int(self.c_expiry_ord[ci]) - int(self.bar_ord[i])
            out = compute_iv_and_greeks(
                None if np.isnan(px) else float(px), spot, float(self.c_strike[ci]),
                t_days / 365.0, self.rate,
                OptionRight.CALL if self.c_is_call[ci] else OptionRight.PUT)
            self._g_iv[i] = np.nan if out["iv"] is None else out["iv"]
            self._g_delta[i] = np.nan if out["delta"] is None else out["delta"]
            self._g_gamma[i] = np.nan if out["gamma"] is None else out["gamma"]
            self._g_theta[i] = np.nan if out["theta"] is None else out["theta"]
            self._g_vega[i] = np.nan if out["vega"] is None else out["vega"]
            self._g_done[i] = True
        return {
            "iv": _f(self._g_iv[i]), "delta": _f(self._g_delta[i]),
            "gamma": _f(self._g_gamma[i]), "theta": _f(self._g_theta[i]),
            "vega": _f(self._g_vega[i]),
        }

    def delta_iv_of_row(self, i: int, ci: int) -> Tuple[Optional[float], Optional[float]]:
        """(delta, iv) only — get_atm_iv's hot path, no dict built."""
        g = self.greeks_of_row(i, ci)
        return g["delta"], g["iv"]

    # -- materialisation ------------------------------------------------
    def bar_dict(self, i: int, ci: int) -> Dict[str, object]:
        """One bar in the dict shape the engine reads off the sqlite store.

        Keys ``open/high/low/close/volume/underlying/option_type/strike/expiry/date`` plus the
        computed ``iv/delta/gamma/theta/vega`` are exactly ``options_cache._BAR_COLS``; the
        parquet-only ``open_interest`` and ``vendor_iv`` are additions nothing reads yet.
        """
        d = dict(self.greeks_of_row(i, ci))
        d.update({
            "occ_symbol": self.c_occ[ci],
            "date": date.fromordinal(int(self.bar_ord[i])).isoformat(),
            "open": _f(self.open[i]), "high": _f(self.high[i]),
            "low": _f(self.low[i]), "close": _f(self.close[i]),
            "volume": _i(self.volume[i]),
            "underlying": self.underlying,
            "option_type": (OptionRight.CALL.value if self.c_is_call[ci]
                            else OptionRight.PUT.value),
            "strike": float(self.c_strike[ci]),
            "expiry": date.fromordinal(int(self.c_expiry_ord[ci])).isoformat(),
            "open_interest": _i(self.open_interest[i]),
            "vendor_iv": _f(self.vendor_iv[i]),
        })
        return d

    def contract(self, i: int, ci: int) -> OptionContract:
        g = self.greeks_of_row(i, ci)
        close = _f(self.close[i])
        # ZERO-SPREAD PREMIUM PROXY, identical in effect to the sqlite store (bid == ask on
        # all 6,757,055 of its rows). The tradeable spread is modelled by option_spread_pct.
        vol = _i(self.volume[i])
        return OptionContract(
            symbol=self.c_occ[ci], underlying=self.underlying,
            option_type=OptionRight.CALL if self.c_is_call[ci] else OptionRight.PUT,
            strike=float(self.c_strike[ci]),
            expiry=date.fromordinal(int(self.c_expiry_ord[ci])),
            bid=close, ask=close, last=close,
            implied_volatility=g["iv"], delta=g["delta"], gamma=g["gamma"],
            theta=g["theta"], vega=g["vega"],
            open_interest=_i(self.open_interest[i]),
            # NO BAR => impossible here (a clamped row is always a bar), but an absent volume
            # is still a KNOWN zero: a bar exists only for a contract that traded. Same rule
            # as options_provider._bar_volume.
            volume=0 if vol is None else vol)


def _f(v) -> Optional[float]:
    return None if v is None or np.isnan(v) else float(v)


def _i(v) -> Optional[int]:
    return None if v is None or np.isnan(v) else int(v)


def _load_underlying(root: str, underlying: str, rate: float,
                     spot_source: Callable[[str, date], Optional[float]]) -> _Underlying:
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    store = OptionHistoryParquetStore(root=root)
    df = store.read_underlying(underlying)
    u = _Underlying(underlying, rate, df, spot_source)
    # COVERAGE, STATED ONCE PER UNDERLYING PER WORKER. The vendor's history FLOOR bounds what
    # COULD have been downloaded; it says nothing about what this tree actually holds, and a
    # run outside the downloaded window reads an empty store and reports the resulting
    # zero-trade result as a result. That is the failure the floor seam exists to prevent, one
    # level down, and it is not detectable from the floor. One log line per underlying is the
    # cheapest honest signal: a 2024 run against a 2023-only tree says so in the first
    # screenful instead of at the post-mortem.
    if u.n_rows:
        logger.info("[backtest] parquet option store: %s %d bars / %d contracts, %s..%s",
                    underlying, u.n_rows, len(u.c_occ),
                    date.fromordinal(int(u.bar_ord.min())).isoformat(),
                    date.fromordinal(int(u.bar_ord.max())).isoformat())
    else:
        logger.warning("[backtest] parquet option store: NO partitions for %s under %s — "
                       "every chain read for it will be empty.", underlying, root)
    return u


def _underlying(root: str, underlying: str, rate: float, spot_scope: str,
                spot_source: Callable[[str, date], Optional[float]]) -> _Underlying:
    key = (root, underlying, rate, spot_scope)
    hist = _WORKER_UNDERLYING_CACHE.get(key)
    if hist is not None:
        _WORKER_UNDERLYING_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _load_underlying(root, underlying, rate, spot_source)
    _WORKER_UNDERLYING_CACHE[key] = hist
    while len(_WORKER_UNDERLYING_CACHE) > _UNDERLYING_CACHE_MAX:
        _WORKER_UNDERLYING_CACHE.popitem(last=False)
    return hist


class ParquetOptionsProvider:
    """The parquet backend of the option-reader seam. See the module docstring."""

    def __init__(self, root: str, *, spot_source: Callable[[str, date], Optional[float]],
                 risk_free_rate: float, spot_scope: str):
        """``spot_scope`` — the identity of what ``spot_source`` will answer.

        REQUIRED, and it is the one non-obvious argument. The worker-level cache holds the
        greeks overlay, and those greeks are a function of the underlying closes the run's
        price source serves. Two runs in the same long-lived pool worker can hold price
        sources over DIFFERENT windows/universes: run A preloaded Jan-Feb, run B Jan-Mar, and
        run B reusing run A's cached ``_Underlying`` would silently invert every March bar
        against a FEBRUARY spot (``close_asof`` forward-fills past the end of what it has).
        The scope makes that a cache miss instead of a wrong number.

        ``build_options_provider`` derives it from the same (universe, interval, window,
        warmup) tuple ``price_source.evict_memo_if_working_set_changed`` keys the OHLCV memo
        on — precisely the tuple over which ``close_asof`` is a pure function. A GA's trials
        share it (same universe, same window), so the cross-trial reuse that makes this
        affordable is unaffected; a different JOB gets fresh entries.
        """
        if not os.path.isdir(root):
            raise OptionsCacheMiss(
                f"No TastyTrade option parquet store at {root}. Build it with "
                f"`python tools/warm_options_history.py` (or point "
                f"BACKTEST_OPTIONS_PARQUET_ROOT at an existing tree). Refusing to run an "
                f"options backtest against an absent store — it would trade nothing and "
                f"report it as a result.")
        self.root = root
        self.spot_source = spot_source
        self.spot_scope = str(spot_scope)
        self.risk_free_rate = float(risk_free_rate)
        #: Parallel to HistoricalOptionsProvider.db_path: the identity this store's worker
        #: caches are keyed on.
        self.store_path = root

    # -- the four engine-facing methods ---------------------------------
    def get_chain(self, underlying: str, as_of: date, *, expiry_min: date, expiry_max: date,
                  option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                  strike_max: Optional[float] = None) -> List[OptionContract]:
        u = self._u(underlying)
        if not u.n_rows:
            return []
        as_of_ord = as_of.toordinal()
        keep = ((u.c_expiry_ord >= expiry_min.toordinal())
                & (u.c_expiry_ord <= expiry_max.toordinal()))
        if option_type is not None:
            keep &= (u.c_is_call if option_type == OptionRight.CALL else ~u.c_is_call)
        if strike_min is not None:
            keep &= (u.c_strike >= strike_min)
        if strike_max is not None:
            keep &= (u.c_strike <= strike_max)
        out: List[OptionContract] = []
        for ci in np.flatnonzero(keep):
            ci = int(ci)
            i = u.latest_row_on_or_before(ci, as_of_ord)
            if i < 0:
                continue  # the contract had not traded yet on/before the clock: not in the chain
            out.append(u.contract(i, ci))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        u = self._u(_underlying_of(occ_symbol))
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.exact_row(ci, as_of.toordinal())
        if i < 0:
            return None
        close = _f(u.close[i])
        # Same zero-spread proxy get_chain uses: entry actions price off chain rows while
        # close actions price off quotes, and the two must agree (options_provider bug B4).
        return OptionQuote(symbol=occ_symbol, bid=close, ask=close, last=close)

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        u = self._u(_underlying_of(occ_symbol))
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.exact_row(ci, as_of.toordinal())
        return None if i < 0 else u.bar_dict(i, ci)

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        """NEAR-ATM implied volatility (0-1), by the SAME rule as the sqlite reader.

        CALLS only, |delta| nearest 0.50 over a 20-45 DTE window, tie-broken by nearest
        expiry then lowest strike; iv and delta both come from the AS-OF-CLAMPED bar's own
        Black-Scholes inversion, with no fallback to anything whose date is unknown. See
        ``options_provider.get_atm_iv`` for why that rule (and its divergence from live) is
        what it is — this reader must not answer a DIFFERENT question from the other backend.
        """
        cache_key = (self.root, underlying, self.risk_free_rate, self.spot_scope,
                     as_of.toordinal())
        cached = _WORKER_ATM_IV_CACHE.get(cache_key, _MISSING)
        if cached is not _MISSING:
            _WORKER_ATM_IV_CACHE.move_to_end(cache_key)  # LRU: mark most-recently-used
            return cached
        result = self._compute_atm_iv(underlying, as_of)
        _WORKER_ATM_IV_CACHE[cache_key] = result
        while len(_WORKER_ATM_IV_CACHE) > _ATM_IV_CACHE_MAX:
            _WORKER_ATM_IV_CACHE.popitem(last=False)
        return result

    # -- internals ------------------------------------------------------
    def _u(self, underlying: str) -> _Underlying:
        return _underlying(self.root, underlying, self.risk_free_rate, self.spot_scope,
                           self.spot_source)

    def _compute_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        u = self._u(underlying)
        if not u.n_rows:
            return None
        as_of_ord = as_of.toordinal()
        lo = (as_of + timedelta(days=_ATM_DTE_MIN)).toordinal()
        hi = (as_of + timedelta(days=_ATM_DTE_MAX)).toordinal()
        keep = u.c_is_call & (u.c_expiry_ord >= lo) & (u.c_expiry_ord <= hi)
        best: Optional[Tuple[Tuple[float, int, float], float]] = None
        for ci in np.flatnonzero(keep):
            ci = int(ci)
            i = u.latest_row_on_or_before(ci, as_of_ord)
            if i < 0:
                continue
            delta, iv = u.delta_iv_of_row(i, ci)
            if delta is None or iv is None:
                continue
            key = (abs(abs(delta) - 0.5), int(u.c_expiry_ord[ci]), float(u.c_strike[ci]))
            if best is None or key < best[0]:
                best = (key, float(iv))
        return best[1] if best is not None else None


@lru_cache(maxsize=1 << 16)
def _underlying_of(occ_symbol: str) -> str:
    """The underlying encoded in an OCC symbol.

    ``get_bar``/``get_quote`` are handed a CONTRACT and the parquet store is partitioned by
    UNDERLYING, so the root has to be recovered from the symbol. The sqlite reader never
    needed this (its ``option_bar`` table carries an ``underlying`` column and is indexed on
    the contract), which is the one place the two backends genuinely differ in shape.

    ``parse_occ`` is the single OCC parser in the codebase and is used verbatim. A symbol it
    refuses is not an OCC contract, so it cannot be in this store: fall back to the
    fixed-width read (everything before the trailing 6 date digits + C/P + 8 strike digits)
    so a synthetic test symbol still routes to a plausible key rather than raising.

    MEMOISED because ``get_bar`` runs it for every held lot on every bar and the symbol set a
    run touches is small and repeats; the regex is otherwise pure overhead on a hot path.
    """
    try:
        return parse_occ(occ_symbol).underlying
    except ValueError:
        s = str(occ_symbol).strip().upper()
        return s[:-15] if len(s) > 15 else s
