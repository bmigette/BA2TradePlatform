"""As-of-clamped reader over the TastyTrade/dxfeed PARQUET option store.

THE SECOND BACKEND, NOT A SECOND ENGINE. ``HistoricalOptionsProvider`` (options_provider.py)
reads the Alpaca-built ``OptionsHistoryCache`` sqlite. This class reads
``CACHE_FOLDER/TastyTradeOptionsProvider/<SYM>/exp=<YYYY-MM-DD>/<SYM>_<exp>_1d.parquet``
(written by ``tools/warm_options_history.py`` via
``ba2_providers.options.parquet_store.OptionHistoryParquetStore``) and exposes the SAME
methods with the SAME signatures and the SAME as-of clamp, so ``BacktestAccount`` cannot tell
them apart. The engine's contract is exactly:

    get_chain(underlying, as_of, *, expiry_min, expiry_max,
              option_type=None, strike_min=None, strike_max=None) -> List[OptionContract]
    get_quote(occ_symbol, as_of)  -> Optional[OptionQuote]
    get_bar(occ_symbol, as_of)    -> Optional[dict]   # EXACT bar on as_of, else None
    get_atm_iv(underlying, as_of) -> Optional[float]
    delta_at_entry(underlying, occ_symbol, when) -> Optional[float]   # results.py refinement

(the first four enumerated from ``backtest_account.py``: get_option_chain / get_option_quote /
get_atm_implied_volatility / ``_options.get_bar`` at the MTM, liquidation, fill,
round-trip-recorder and expiry-settlement sites; the fifth from
``results._build_refine_drawdown_fn`` — see DELTA-AT-ENTRY below.)

WHICH STORE A RUN READS IS AN EXPLICIT CHOICE THAT DEFAULTS TO SQLITE — see
``options_store.py``. Every backtest number on record was produced against the sqlite path
and nothing here may perturb it.

WHAT THE PARQUET HAS THAT THE SQLITE DOES NOT
  * ``open_interest`` — POPULATED (26,853 of 27,974 GOOG rows). The sqlite's is NULL on every
    one of its 1,440,782 chain rows (re-measured 2026-08-31; see
    ``option_selector._publishes_spread`` for the full record), so ``option_selector``'s
    ``min_open_interest`` gate is un-answerable there and becomes usable here. This is the
    ONE field that is genuinely dead in the sqlite -- its ``iv``/``delta`` are populated on
    46% of chain rows and 88% of BAR rows, so do not extend this bullet to them.
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
    than sqlite, where ``bid == ask`` on every quoted row (0 of 1,083,571 have ask > bid;
    the other 357,211 of 1,440,782 chain rows carry no quote at all): both stores are a
    ZERO-SPREAD premium proxy and the tradeable spread is MODELLED downstream by
    ``option_spread_pct``. CONSEQUENCE FOR RANKING, since it reads backwards at a glance:
    a constant 0.0 spread is not "no signal", it is the BEST possible score --
    ``option_selection_policy._minimise`` maps a degenerate column to 0.0 and inverts it to
    1.0 for every candidate. ``w_spread`` therefore fails OPEN uniformly here, which is why
    the grid withholds it (see the launcher's ``_OPTION_SELECTION_WEIGHT_BANDS``).
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

THE SPOT SOURCE LIVES ON THE PROVIDER, NOT IN THE CACHE. It is a closure over the RUN's
``AsOfPriceSource`` (``options_store.price_source_spot``), and that price source owns the
run's whole OHLCV memo. Nothing on the run path clears the worker caches
(``clear_worker_parquet_options_cache`` has no production caller — it is test isolation), so
a cached object holding that closure would pin the finished run's price source, and its memo,
for the life of the pool worker. It is therefore threaded through
``greeks_tuple``/``bar_dict``/``contract`` as an argument instead of stored on the cached
overlay. What the overlay caches is the RESULT (a float close per bar date), which is inert.

AS-OF CLAMP (the whole point). The store is one row per contract per bar_date, so "the chain
on date D" is derived, not stored: the contract UNIVERSE on D is every contract with at least
one bar dated <= D, and each contract's row is its LATEST bar <= D. A contract whose first
bar is after D is not in the chain at all. That is the same shape as the sqlite reader
(``latest_as_of`` snapshot + per-contract ``latest_on_or_before`` overlay) with the snapshot
derived instead of stored, and like it, a stale-but-clamped bar is preferred to no row — the
fill engine still requires an EXACT bar on the fill day, so an untraded contract cannot fill.

DELTA-AT-ENTRY IS A NAMED SEAM METHOD, NOT AN INCIDENTAL ATTRIBUTE.
``results._build_refine_drawdown_fn`` needs one option-specific fact — the delta a contract
carried when the trade was entered — to refine intraday drawdown. It used to reach for
``options.cache.db_path``, an attribute only the sqlite reader has, so on this backend the
refinement silently switched itself off: no log, and a DIFFERENT ``max_drawdown`` (hence a
different ``option_consistent_annual_return`` fitness) for reasons invisible from the result.
Both readers now implement ``delta_at_entry`` and the refinement follows the READER.

CACHING — TWO CACHES, BECAUSE THE BYTES AND THE GREEKS HAVE DIFFERENT KEYS. A GA rebuilds the
provider once per trial from the same store and re-evaluates identical (symbol, date) pairs,
so reads are cached at WORKER-PROCESS level, not per instance:

  * ``_WORKER_RAW_CACHE`` — one ``_RawUnderlying`` per (root, underlying). The parquet bytes,
    parsed into columnar numpy: prices, volumes, the per-contract descriptors, and the
    ordinal/ISO/date lookups derived from them. NOTHING here depends on the run.
  * ``_WORKER_UNDERLYING_CACHE`` — one ``_Underlying`` overlay per (root, underlying, rate,
    spot_scope), holding a reference to the raw plus the lazily-filled greeks arrays, the
    per-bar spot memo, and the materialised-bar-dict memo. These ARE a function of the run.
  * ``_WORKER_ATM_IV_CACHE`` — the get_atm_iv RESULT memo, mirroring the sqlite reader's.

WHY THE SPLIT IS NOT COSMETIC. ``spot_scope`` is derived from (universe, interval, window,
warmup) and ``_build_daily_trial_config`` sets ``enabled_instruments`` PER INDIVIDUAL, to the
screener candidates that trial's own genes selected — so in a screener GA the scope changes
between trials of the same job. Keyed as one object, a scope change re-read and re-parsed
BYTE-IDENTICAL parquet (measured: GOOG 145 ms cold, 4.5 us warm, 58 ms on a new scope) and
left two full copies in the 200-entry LRU. The expensive part — the I/O plus
``_iso_to_ordinal_array``'s per-row ``date.fromisoformat`` loop — is scope-INDEPENDENT, so it
belongs in a scope-independent cache. The scope key still guards exactly what it was
introduced to guard (see ``ParquetOptionsProvider.__init__``): two runs whose price sources
answer differently for the same (symbol, date) get different GREEKS, they just stop paying to
re-read the same bytes to find that out.

All three are bounded LRUs (a remote worker's pool is long-lived across jobs touching
different universes) and all three are dropped by ``clear_worker_parquet_options_cache()``,
which ``options_provider.clear_worker_options_cache()`` also calls so existing test isolation
covers this store too. An overlay holds a strong reference to its raw, so evicting a raw
while an overlay still uses it frees nothing and breaks nothing — the two caps are equal and
the keys are parallel, so they evict roughly together.

Columnar (numpy) rather than dict-per-bar because the cap has to clear a realistic universe:
686 underlyings x ~28k rows, and a run's ~100-symbol universe must fit in a worker alongside
the OHLCV memo. Measured on GOOG (27,974 rows / 1,374 contracts): 2.36 MB for the raw
(1.53 MB of numpy plus 0.43 MB of the interned python projections the hot paths index and
0.26 MB of the contract symbol list/index) and 1.09 MB for one greeks overlay. Bars are
materialised into dicts only when a caller actually reads one, and then memoised (see
``bar_dict``).
"""
from __future__ import annotations

import logging
import os
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from ba2_providers.options.tastytrade import parse_occ

from .option_greeks import compute_iv_and_greeks
from .options_cache import OptionsCacheMiss

logger = logging.getLogger(__name__)

#: Underlyings held per worker process. Measured: GOOG's 27,974 rows cost 2.36 MB raw plus
#: 1.09 MB per greeks overlay, so the default cap is ~700 MB worst case with both caches full
#: and clears a 100-symbol option universe several times over. Sizing it BELOW the run's
#: universe is the thing to avoid — that thrashes (evict-then-reload) inside a single bar,
#: exactly as the sqlite reader's bar-cache comment warns. The penalty is far gentler here
#: than there (a reload is ~55 ms of parquet, not thousands of sqlite round-trips), so
#: lowering it on a memory-tight worker is a real option. The same cap bounds BOTH the raw
#: cache and the scope-keyed overlay cache.
_UNDERLYING_CACHE_MAX = int(os.getenv("BT_OPTION_PARQUET_CACHE_MAX", "200"))
#: Same generosity (and same reasoning) as the sqlite reader's ATM-IV memo: the values are a
#: float or None, and a GA re-asks the identical (symbol, date) pairs on every trial.
_ATM_IV_CACHE_MAX = int(os.getenv("BT_OPTION_ATM_IV_CACHE_MAX", "200000"))

#: SCOPE-INDEPENDENT. The parquet bytes and everything derived from them alone. See CACHING.
_WORKER_RAW_CACHE: "OrderedDict[Tuple[str, str], _RawUnderlying]" = OrderedDict()
# The overlay keys carry SPOT_SCOPE (see ParquetOptionsProvider.__init__): the cached greeks
# are a function of the underlying closes the run's price source serves, and two runs over
# different windows/universes do not serve the same ones. The RAW bars they sit on are the
# same bytes either way, which is why they are a separate cache.
_WORKER_UNDERLYING_CACHE: "OrderedDict[Tuple[str, str, float, str], _Underlying]" = OrderedDict()
_WORKER_ATM_IV_CACHE: "OrderedDict[Tuple[str, str, float, str, int], Optional[float]]" = OrderedDict()

#: Distinct from None, which is a VALID cached result. See options_provider._MISSING.
_MISSING = object()

#: ATM window, identical to the sqlite reader's so the two produce the same statistic.
_ATM_DTE_MIN = 20
_ATM_DTE_MAX = 45

#: greeks_tuple's shape, for readers of the hot paths. (iv, delta, gamma, theta, vega).
_NO_GREEKS: Tuple[Optional[float], ...] = (None, None, None, None, None)


def clear_worker_parquet_options_cache() -> None:
    """Drop every cached underlying + ATM-IV result (test isolation / explicit reset)."""
    _WORKER_RAW_CACHE.clear()
    _WORKER_UNDERLYING_CACHE.clear()
    _WORKER_ATM_IV_CACHE.clear()
    _underlying_of.cache_clear()


def _iso_to_ordinal_array(series) -> np.ndarray:
    """'YYYY-MM-DD' strings -> proleptic-Gregorian ordinals (int32).

    Ordinals, not YYYYMMDD: they are monotone in date (so ``searchsorted`` is the as-of clamp)
    AND their difference is a day count, which is exactly what the Black-Scholes ``T`` needs.

    The per-row ``date.fromisoformat`` loop is the second-biggest cost of a cold load after
    the parquet read itself — and, like the read, it depends on nothing but the bytes, which
    is why ``_RawUnderlying`` (where it lands) is cached without the run's spot scope.
    """
    return np.array([date.fromisoformat(str(s)).toordinal() for s in series], dtype=np.int32)


class _RawUnderlying:
    """One underlying's whole parquet history, columnar. SCOPE-INDEPENDENT — see CACHING.

    Rows are sorted by (occ_symbol, bar_date); ``starts[i]:stops[i]`` is contract ``i``'s
    slice, so an as-of clamp is a ``searchsorted`` inside that slice.

    Everything a bar or a chain row needs that is NOT a greek is precomputed here once:
    ``iso_of_ord`` (there are ~60 distinct bar dates in a quarter, not 28,000) and the
    per-contract ``c_expiry_date`` / ``c_expiry_iso`` / ``c_type_str`` / ``c_strike_f``
    lists, all as native Python objects. ``date.fromordinal(...).isoformat()`` is 0.22 us and
    ``float(np.float64)`` is 0.023 us against 0.016 us for a list index — small individually,
    and the whole reason ``bar_dict`` used to cost 5.4 us.
    """

    __slots__ = (
        "underlying", "n_rows",
        "c_occ", "c_index", "c_strike", "c_strike_f", "c_expiry_ord", "c_expiry_ord_l",
        "c_is_call", "c_expiry_date", "c_expiry_iso", "c_type_str", "c_right",
        "starts", "stops", "starts_l", "stops_l",
        "bar_ord", "bar_ord_l", "open", "high", "low", "close", "volume", "open_interest",
        "vendor_iv", "iso_of_ord", "date_of_ord", "bid", "ask", "has_quotes",
    )

    def __init__(self, underlying: str, df):
        self.underlying = underlying
        self.iso_of_ord: Dict[int, str] = {}
        self.date_of_ord: Dict[int, date] = {}

        if df is None or not len(df):
            self.n_rows = 0
            self.c_occ = []
            self.c_index = {}
            self.c_strike_f = []
            self.c_expiry_date = []
            self.c_expiry_iso = []
            self.c_type_str = []
            self.c_right = []
            self.c_expiry_ord_l = []
            self.starts_l = []
            self.stops_l = []
            self.bar_ord_l = []
            self.has_quotes = False
            for name in ("c_strike", "c_expiry_ord", "c_is_call", "starts", "stops", "bar_ord",
                         "open", "high", "low", "close", "volume", "open_interest", "vendor_iv",
                         "bid", "ask"):
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

        # REAL QUOTES, when the store has them. The TastyTrade tree predates the bid/ask
        # columns entirely and its partitions do not carry them; ThetaData's do. Absent
        # columns => all-NaN => `contract()` falls back to the historical zero-spread close
        # proxy, so a TastyTrade-backed run is byte-identical to before this existed.
        self.has_quotes = "bid" in df.columns and "ask" in df.columns
        if self.has_quotes:
            self.bid = df["bid"].to_numpy(dtype="float64", na_value=np.nan)
            self.ask = df["ask"].to_numpy(dtype="float64", na_value=np.nan)
        else:
            self.bid = np.full(n, np.nan)
            self.ask = np.full(n, np.nan)

        # INVARIANT: every stored row has a price -- a trade close, or a quote, or both. A row
        # with neither cannot be priced, and (per option_selector.passes_liquidity) a contract
        # whose mark is None SKIPS the penny-contract gate instead of being rejected by it, so
        # it would survive selection unpriced. Providers drop such rows at ingest; this counts
        # them once per underlying rather than per contract, so a store that violates the
        # invariant says so loudly instead of quietly mis-selecting.
        priceless = int(np.count_nonzero(
            np.isnan(self.close) & np.isnan(self.bid) & np.isnan(self.ask)))
        if priceless:
            logger.error(
                "%s: %d of %d option bar rows have NO price at all (no close, no bid, no "
                "ask). These cannot be marked or liquidity-gated. The store is malformed -- "
                "re-warm this underlying.", underlying, priceless, n)

        # -- native-Python projections, built once (measured 1.5 ms for GOOG's 27,974 rows /
        #    1,374 contracts, against a ~130 ms parquet read) ------------------------------
        # bar_ord as a LIST as well as an array: the as-of clamp is a bisect, and
        # ``bisect_right(list, x, lo, hi)`` is 0.051 us against 0.73 us for
        # ``np.searchsorted(arr[lo:hi], x)`` (which allocates a view and pays numpy's call
        # overhead). get_chain runs that clamp ONCE PER CONTRACT — 1,374 times for GOOG — and
        # get_atm_iv once per contract in the DTE band, so it is not a micro-optimisation.
        # INTERNED (setdefault) because there are ~82 distinct ordinals here, not 27,974: a
        # plain ``.tolist()`` would allocate 27,974 separate int objects (+0.9 MB/underlying,
        # ~30% on top of the columnar arrays) to hold 82 distinct values.
        seen: Dict[int, int] = {}
        self.bar_ord_l = [seen.setdefault(v, v) for v in self.bar_ord.tolist()]
        self.starts_l = self.starts.tolist()
        self.stops_l = self.stops.tolist()
        date_of_ord = {int(o): date.fromordinal(int(o))
                       for o in np.unique(np.concatenate([self.bar_ord, self.c_expiry_ord]))}
        self.date_of_ord = date_of_ord
        self.iso_of_ord = {o: d.isoformat() for o, d in date_of_ord.items()}
        self.c_expiry_ord_l = self.c_expiry_ord.tolist()
        self.c_expiry_date = [date_of_ord[o] for o in self.c_expiry_ord_l]
        self.c_expiry_iso = [self.iso_of_ord[o] for o in self.c_expiry_ord_l]
        self.c_strike_f = self.c_strike.tolist()
        self.c_right = [OptionRight.CALL if c else OptionRight.PUT
                        for c in self.c_is_call.tolist()]
        self.c_type_str = [r.value for r in self.c_right]


class _Underlying:
    """A run-scoped greeks/spot/bar overlay over one cached ``_RawUnderlying``.

    Keyed on (root, underlying, rate, spot_scope) — everything in here is a function of the
    run's underlying closes and its Black-Scholes rate, and nothing in here is a function of
    the parquet bytes, which the raw already holds exactly once.

    The raw's columnar arrays are re-bound onto this object's own slots at construction (a
    handful of pointer copies) so every clamp/read stays a single attribute load rather than
    a two-hop ``self.raw.close[i]`` on paths that run per contract per bar.
    """

    __slots__ = (
        "raw", "underlying", "rate", "n_rows",
        "c_occ", "c_index", "c_strike", "c_expiry_ord", "c_is_call", "starts", "stops",
        "bar_ord", "bar_ord_l", "starts_l", "stops_l",
        "open", "high", "low", "close", "volume", "open_interest", "vendor_iv", "bid", "ask",
        "_g_done", "_g_iv", "_g_delta", "_g_gamma", "_g_theta", "_g_vega",
        "_spot_cache", "_bar_memo",
    )

    def __init__(self, raw: "_RawUnderlying", rate: float):
        self.raw = raw
        self.underlying = raw.underlying
        self.rate = float(rate)
        self._spot_cache: Dict[int, Optional[float]] = {}
        self._bar_memo: Dict[int, Dict[str, object]] = {}

        n = raw.n_rows
        self.n_rows = n
        for name in ("c_occ", "c_index", "c_strike", "c_expiry_ord", "c_is_call",
                     "starts", "stops", "starts_l", "stops_l", "bar_ord", "bar_ord_l",
                     "open", "high", "low", "close", "volume", "open_interest", "vendor_iv",
                     "bid", "ask"):
            setattr(self, name, getattr(raw, name))

        self._g_done = np.zeros(n, dtype=bool)
        for name in ("_g_iv", "_g_delta", "_g_gamma", "_g_theta", "_g_vega"):
            setattr(self, name, np.full(n, np.nan, dtype="float64"))

    # -- as-of clamp ----------------------------------------------------
    # ``bisect`` over the interned python list rather than ``np.searchsorted`` over an array
    # SLICE: identical answers (the rows are sorted by (occ_symbol, bar_date), so contract
    # ci's bar ordinals ascend across starts_l[ci]:stops_l[ci]) at 0.05 us instead of 0.73 us,
    # and this runs once per contract inside every get_chain / get_atm_iv.
    def latest_row_on_or_before(self, ci: int, as_of_ord: int) -> int:
        """Row index of contract ``ci``'s latest bar dated <= ``as_of_ord``, or -1."""
        lo = self.starts_l[ci]
        j = bisect_right(self.bar_ord_l, as_of_ord, lo, self.stops_l[ci])
        return j - 1 if j > lo else -1

    def exact_row(self, ci: int, as_of_ord: int) -> int:
        """Row index of contract ``ci``'s bar dated EXACTLY ``as_of_ord``, or -1."""
        hi = self.stops_l[ci]
        j = bisect_left(self.bar_ord_l, as_of_ord, self.starts_l[ci], hi)
        if j >= hi or self.bar_ord_l[j] != as_of_ord:
            return -1
        return j

    # -- greeks ---------------------------------------------------------
    def _spot_on(self, bar_ord: int, spot_source) -> Optional[float]:
        """The run's close for this bar's date, memoised per DATE (not per row).

        The memo holds a float, never the source: see the module docstring on why the spot
        source itself must not end up in a worker-lifetime cache.
        """
        v = self._spot_cache.get(bar_ord, _MISSING)
        if v is _MISSING:
            v = spot_source(self.underlying, self.raw.date_of_ord[bar_ord])
            self._spot_cache[bar_ord] = v
        return v

    def greeks_tuple(self, i: int, ci: int, spot_source) -> Tuple[Optional[float], ...]:
        """(iv, delta, gamma, theta, vega) for row ``i``, inverted from its own close.

        Memoised per row for the life of the cached overlay: a GA re-reads the same
        (contract, bar) pairs on every trial, and get_atm_iv alone re-scans a whole DTE band
        per bar. ``compute_iv_and_greeks`` is the ONE greeks path (11.2 us/call measured), the
        same one ``fetch_options.bar_to_row`` used to fill the sqlite store's bars.

        A TUPLE, not a dict, because every caller of this is per-contract-per-bar. Rebuilding
        a 5-key dict here cost 1.7 us on a MEMO HIT — 0.27 us of it per ``np.isnan`` on a
        numpy scalar, which is why ``_f`` now tests ``v != v`` instead (0.019 us).
        """
        if not self._g_done[i]:
            px = self.close[i]
            bar_ord = self.bar_ord_l[i]
            spot = self._spot_on(bar_ord, spot_source)
            t_days = self.raw.c_expiry_ord_l[ci] - bar_ord
            out = compute_iv_and_greeks(
                None if px != px else float(px), spot, self.raw.c_strike_f[ci],
                t_days / 365.0, self.rate, self.raw.c_right[ci])
            self._g_iv[i] = np.nan if out["iv"] is None else out["iv"]
            self._g_delta[i] = np.nan if out["delta"] is None else out["delta"]
            self._g_gamma[i] = np.nan if out["gamma"] is None else out["gamma"]
            self._g_theta[i] = np.nan if out["theta"] is None else out["theta"]
            self._g_vega[i] = np.nan if out["vega"] is None else out["vega"]
            self._g_done[i] = True
        return (_f(self._g_iv[i]), _f(self._g_delta[i]), _f(self._g_gamma[i]),
                _f(self._g_theta[i]), _f(self._g_vega[i]))

    def delta_iv_of_row(self, i: int, ci: int, spot_source
                        ) -> Tuple[Optional[float], Optional[float]]:
        """(delta, iv) only — get_atm_iv's hot path."""
        g = self.greeks_tuple(i, ci, spot_source)
        return g[1], g[0]

    # -- materialisation ------------------------------------------------
    def bar_dict(self, i: int, ci: int, spot_source) -> Dict[str, object]:
        """One bar in the dict shape the engine reads off the sqlite store.

        Keys ``open/high/low/close/volume/underlying/option_type/strike/expiry/date`` plus the
        computed ``iv/delta/gamma/theta/vega`` are exactly ``options_cache._BAR_COLS``; the
        parquet-only ``open_interest`` and ``vendor_iv`` are additions nothing reads yet.

        MEMOISED PER ROW, and the memo is what makes this affordable: ``get_bar`` is called
        for every held lot on every bar (MTM, liquidation, fill, expiry settlement) and a row
        is IMMUTABLE once the underlying is cached, so the same 17-key dict was being rebuilt
        — two ``date.fromordinal().isoformat()`` conversions, seven ``np.isnan`` NaN tests and
        a fresh dict — every single time. Measured 5.4 us standalone; a memo hit plus the
        ``copy()`` below is ~0.1 us.

        A COPY, not the memo itself: callers get a dict and none of them currently mutate it,
        but handing out the cached object would make that a silent cross-call corruption
        rather than a local bug, and ``dict.copy()`` on 17 keys is 0.054 us.
        """
        d = self._bar_memo.get(i)
        if d is None:
            raw = self.raw
            iv, delta, gamma, theta, vega = self.greeks_tuple(i, ci, spot_source)
            d = {
                "iv": iv, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
                "occ_symbol": raw.c_occ[ci],
                "date": raw.iso_of_ord[self.bar_ord_l[i]],
                "open": _f(self.open[i]), "high": _f(self.high[i]),
                "low": _f(self.low[i]), "close": _f(self.close[i]),
                "volume": _i(self.volume[i]),
                "underlying": self.underlying,
                "option_type": raw.c_type_str[ci],
                "strike": raw.c_strike_f[ci],
                "expiry": raw.c_expiry_iso[ci],
                "open_interest": _i(self.open_interest[i]),
                "vendor_iv": _f(self.vendor_iv[i]),
            }
            self._bar_memo[i] = d
        return d.copy()

    def contract(self, i: int, ci: int, spot_source) -> OptionContract:
        raw = self.raw
        iv, delta, gamma, theta, vega = self.greeks_tuple(i, ci, spot_source)
        close = _f(self.close[i])
        bid, ask = _f(self.bid[i]), _f(self.ask[i])
        if bid is None or ask is None:
            # ZERO-SPREAD PREMIUM PROXY -- the pre-2026-09 behaviour, still used for stores
            # with no quote columns (the whole TastyTrade tree) and identical in effect to the
            # sqlite store (bid == ask on every one of its quoted rows). It makes spread_pct a
            # constant 0.0, which RANKS BEST rather than not ranking, so max_spread_pct gates
            # nothing and w_spread scores every candidate alike -- see the module docstring's
            # bid/ask bullet. A store WITH real quotes (ThetaData) takes the branch above and
            # those two knobs start working.
            bid = ask = close
        vol = _i(self.volume[i])
        return OptionContract(
            symbol=raw.c_occ[ci], underlying=self.underlying,
            option_type=raw.c_right[ci],
            strike=raw.c_strike_f[ci],
            expiry=raw.c_expiry_date[ci],
            # `last` stays the TRADE price and is None on a day the contract did not trade;
            # the mark for such a row is the quote mid, which OptionContract.mid derives from
            # the real bid/ask above. Never substitute the mid into `last` -- callers use the
            # two to tell an actual print from a quote.
            bid=bid, ask=ask, last=close,
            implied_volatility=iv, delta=delta, gamma=gamma, theta=theta, vega=vega,
            open_interest=_i(self.open_interest[i]),
            # NO BAR => impossible here (a clamped row is always a bar), but an absent volume
            # is still a KNOWN zero: a bar exists only for a contract that traded. Same rule
            # as options_provider._bar_volume.
            volume=0 if vol is None else vol)


def _f(v) -> Optional[float]:
    """numpy scalar -> float, NaN -> None.

    ``v != v`` rather than ``np.isnan(v)``: identical on every float and 14x cheaper
    (0.019 us vs 0.272 us measured on a np.float64), and this runs 12x per materialised bar.
    """
    return None if v is None or v != v else float(v)


def _i(v) -> Optional[int]:
    return None if v is None or v != v else int(v)


def _load_raw_underlying(root: str, underlying: str) -> "_RawUnderlying":
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    store = OptionHistoryParquetStore(root=root)
    df = store.read_underlying(underlying)
    u = _RawUnderlying(underlying, df)
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


def _raw_underlying(root: str, underlying: str) -> "_RawUnderlying":
    """The parquet bytes for (root, underlying), read at most once per worker.

    NO spot scope and NO rate in this key — see the module docstring's CACHING section. This
    is the cache that stops a screener GA (whose ``enabled_instruments``, and therefore whose
    ``spot_scope``, changes per individual) from re-reading identical bytes per trial.
    """
    key = (root, underlying)
    raw = _WORKER_RAW_CACHE.get(key)
    if raw is not None:
        _WORKER_RAW_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return raw
    raw = _load_raw_underlying(root, underlying)
    _WORKER_RAW_CACHE[key] = raw
    while len(_WORKER_RAW_CACHE) > _UNDERLYING_CACHE_MAX:
        _WORKER_RAW_CACHE.popitem(last=False)
    return raw


def _underlying(root: str, underlying: str, rate: float, spot_scope: str) -> _Underlying:
    """The run-scoped greeks/bar overlay for (root, underlying, rate, spot_scope)."""
    key = (root, underlying, rate, spot_scope)
    hist = _WORKER_UNDERLYING_CACHE.get(key)
    if hist is not None:
        _WORKER_UNDERLYING_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _Underlying(_raw_underlying(root, underlying), rate)
    _WORKER_UNDERLYING_CACHE[key] = hist
    while len(_WORKER_UNDERLYING_CACHE) > _UNDERLYING_CACHE_MAX:
        _WORKER_UNDERLYING_CACHE.popitem(last=False)
    return hist


def _as_date(when: Any) -> Optional[date]:
    """A date/datetime/ISO string -> date. The refinement seam is handed all three."""
    if when is None:
        return None
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    try:
        return datetime.fromisoformat(str(when)[:19].replace(" ", "T")).date()
    except ValueError:
        return None


class ParquetOptionsProvider:
    """The parquet backend of the option-reader seam. See the module docstring."""

    def __init__(self, root: str, *, spot_source: Callable[[str, date], Optional[float]],
                 risk_free_rate: float, spot_scope: str):
        """``spot_scope`` — the identity of what ``spot_source`` will answer.

        REQUIRED, and it is the one non-obvious argument. The worker-level cache holds the
        greeks overlay, and those greeks are a function of the underlying closes the run's
        price source serves. Two runs in the same long-lived pool worker can hold price
        sources over DIFFERENT windows/universes: run A preloaded Jan-Feb, run B Jan-Mar, and
        run B reusing run A's cached overlay would silently invert every March bar against a
        FEBRUARY spot (``close_asof`` forward-fills past the end of what it has). The scope
        makes that a cache miss instead of a wrong number.

        It keys the GREEKS ONLY. The parquet bytes underneath are the same bytes for every
        scope and are cached separately on (root, underlying); see the module docstring.

        ``build_options_provider`` derives it from the same (universe, interval, window,
        warmup) tuple ``price_source.evict_memo_if_working_set_changed`` keys the OHLCV memo
        on — precisely the tuple over which ``close_asof`` is a pure function. A GA's trials
        share it when the universe is fixed; a screener GA varies ``enabled_instruments`` per
        individual and so varies the scope too, which is exactly the case the byte-level cache
        below now absorbs.
        """
        if not os.path.isdir(root):
            raise OptionsCacheMiss(
                f"No TastyTrade option parquet store at {root}. Build it with "
                f"`python tools/warm_options_history.py` (or point "
                f"BACKTEST_OPTIONS_PARQUET_ROOT at an existing tree). Refusing to run an "
                f"options backtest against an absent store — it would trade nothing and "
                f"report it as a result.")
        self.root = root
        #: HELD HERE, NOT IN THE WORKER CACHE — it closes over the run's AsOfPriceSource (and
        #: therefore that run's whole OHLCV memo), and this object dies with the run while the
        #: caches outlive it. See the module docstring.
        self.spot_source = spot_source
        self.spot_scope = str(spot_scope)
        self.risk_free_rate = float(risk_free_rate)
        #: Parallel to HistoricalOptionsProvider.db_path: the identity this store's worker
        #: caches are keyed on.
        self.store_path = root

    # -- the engine-facing methods --------------------------------------
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
        spot_source = self.spot_source
        out: List[OptionContract] = []
        for ci in np.flatnonzero(keep):
            ci = int(ci)
            i = u.latest_row_on_or_before(ci, as_of_ord)
            if i < 0:
                continue  # the contract had not traded yet on/before the clock: not in the chain
            out.append(u.contract(i, ci, spot_source))
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
        bid, ask = _f(u.bid[i]), _f(u.ask[i])
        if bid is None or ask is None:
            bid = ask = close
        # Same pricing rule get_chain's ``contract()`` uses, and it MUST stay a twin: entry
        # actions price off chain rows while close actions price off quotes, and the two must
        # agree (options_provider bug B4). Real quotes when the store has them, the
        # zero-spread close proxy when it does not; `last` is the trade print either way.
        #
        # GREEKS TOO (plan Task 6), from the SAME per-row inversion ``get_chain`` uses -- the
        # sqlite reader's twin change, and it must be a twin or a rule that reads a held
        # contract's delta answers differently on the two backends. Memoised per row by
        # ``greeks_tuple``, so a quote costs no extra inversion once the chain has priced it.
        delta, iv = u.delta_iv_of_row(i, ci, self.spot_source)
        return OptionQuote(symbol=occ_symbol, bid=bid, ask=ask, last=close,
                           delta=delta, implied_volatility=iv)

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        u = self._u(_underlying_of(occ_symbol))
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.exact_row(ci, as_of.toordinal())
        return None if i < 0 else u.bar_dict(i, ci, self.spot_source)

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

    def delta_at_entry(self, underlying: str, occ_symbol: str, when: Any) -> Optional[float]:
        """The contract's delta AS OF ``when`` — the intraday-drawdown refinement's one
        option-specific input (``results._build_refine_drawdown_fn``).

        Same as-of discipline as ``get_chain``: the contract's LATEST bar on or before that
        date, never a later one. ``when`` may be a datetime (what the refinement holds), a
        date, or an ISO string; an unparseable one is None rather than an exception, because
        this is a refinement and must never fail a finished run.

        Deliberately NOT ``get_chain(...)``-then-search: the refinement asks about ONE named
        contract, and building a whole chain (and every greek in it) to read one delta is what
        makes a refinement expensive enough to be worth switching off.
        """
        d = _as_date(when)
        if d is None:
            return None
        u = self._u(underlying)
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.latest_row_on_or_before(ci, d.toordinal())
        if i < 0:
            return None
        return u.greeks_tuple(i, ci, self.spot_source)[1]

    # -- internals ------------------------------------------------------
    def _u(self, underlying: str) -> _Underlying:
        return _underlying(self.root, underlying, self.risk_free_rate, self.spot_scope)

    def _compute_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        u = self._u(underlying)
        if not u.n_rows:
            return None
        as_of_ord = as_of.toordinal()
        lo = (as_of + timedelta(days=_ATM_DTE_MIN)).toordinal()
        hi = (as_of + timedelta(days=_ATM_DTE_MAX)).toordinal()
        keep = u.c_is_call & (u.c_expiry_ord >= lo) & (u.c_expiry_ord <= hi)
        spot_source = self.spot_source
        best: Optional[Tuple[Tuple[float, int, float], float]] = None
        for ci in np.flatnonzero(keep):
            ci = int(ci)
            i = u.latest_row_on_or_before(ci, as_of_ord)
            if i < 0:
                continue
            delta, iv = u.delta_iv_of_row(i, ci, spot_source)
            if delta is None or iv is None:
                continue
            key = (abs(abs(delta) - 0.5), u.raw.c_expiry_ord_l[ci], u.raw.c_strike_f[ci])
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
